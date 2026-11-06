"""事件审计日志中间件：全量记录本进程**发射**的事件，自带垃圾回收。

`blue` 需要回答"某个决定是被哪条事件触发的"，但总线上事件的因果链
（`Event.event_ids` / `sources` / `timestamps`）在投递完成、消息被 ack 之后就
消失了。本中间件把这些元数据落到每个服务自己的 SQLite 里，作为**数据面**的
审计记录。

与库自带的 `SQLiteLoggingMiddleware` 的差别：

========================  ==========================  ============================
关注点                     `SQLiteLoggingMiddleware`   `JournalMiddleware`
========================  ==========================  ============================
写入时机                  每条一次 commit              攒批 + 定时刷新（单事务）
失败处理                  一次失败后永久降级            退避重连，可自动恢复
负载存储                  全量 `data`                  默认只存 `size` + `sha256`
发布失败                  只进 fallback                落 `error` 列
保留策略                  无                           retention / max_rows / max_bytes
垃圾回收                  无                           定期 sweep + `incremental_vacuum`
========================  ==========================  ============================

**位置约束（重要）**
本中间件必须挂在 `on_publish`（发射侧）。总线是 fanout——"任一发布 → 所有在线
成员收到（含自收）"，若改挂 `before_dispatch`，同一事件会被每个成员各记一份，
25 个服务就是 25 倍重复。发射侧天然满足"一个事件只有一个发射者"，因此这里是
每事件一次。

**不阻塞、不抛出**
发布路径上只做一次 `put_nowait`；写库与 GC 全在后台任务里完成。缓冲满、磁盘
满、库损坏都只降级并计数，绝不影响业务事件——审计是观测，不是正确性依赖。

**记录的语义**
`on_publish` 在 `EventBus._core_publish` 的尾部触发，此时事件**已经**通过
`EventQueue.put` 交给传输层，因此这一行代表"已发射"的事实；传输失败走
`on_publish_error`，落一行带 `error` 的记录。两者合起来覆盖发射侧的全部结局。

**数据面**
日志库永不回滚：`blue rollback` 不恢复历史事件。见 `docs/generations.md` §2。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from infinity_bus import (
    BeforePublishNext,
    Event,
    EventBus,
    EventRegistry,
    Middleware,
    OnPublishNext,
)
from pydantic import BaseModel

__all__ = [
    "JOURNAL_DIRNAME",
    "JournalMiddleware",
    "JournalRow",
    "payload_digest",
]

JOURNAL_DIRNAME = "journal"
"""审计库在各成员状态目录下的子目录名（与 `dedupe` 平级）。"""

logger = logging.getLogger(__name__)


# 一行日志的列顺序，与 `_INSERT_SQL` 严格对齐。
#   ts / emitted_at / service / name / event_id / event_ids / sources /
#   timestamps / payload_size / payload_sha / payload / error
JournalRow = tuple[
    float,  # ts            写入时刻（unix 秒）
    float | None,  # emitted_at    最后一个 timestamps 的 unix 秒
    str,  # service       发射者
    str,  # name          事件名
    str,  # event_id      事件 UUID
    str,  # event_ids     因果链（JSON 数组）
    str,  # sources       处理链（JSON 数组）
    str,  # timestamps    逐跳时间戳（JSON 数组）
    int,  # payload_size  负载字节数
    str | None,  # payload_sha   负载摘要
    str | None,  # payload       负载正文（仅 full 模式 / 白名单 / 未超限）
    str | None,  # error         发布失败时的异常描述
]

_DDL = """\
CREATE TABLE IF NOT EXISTS {table} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    emitted_at   REAL,
    service      TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    event_id     TEXT    NOT NULL,
    event_ids    TEXT    NOT NULL DEFAULT '[]',
    sources      TEXT    NOT NULL DEFAULT '[]',
    timestamps   TEXT    NOT NULL DEFAULT '[]',
    payload_size INTEGER NOT NULL DEFAULT 0,
    payload_sha  TEXT,
    payload      TEXT,
    error        TEXT
)"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS {table}_ts ON {table}(ts)",
    "CREATE INDEX IF NOT EXISTS {table}_event ON {table}(event_id)",
    "CREATE INDEX IF NOT EXISTS {table}_name_ts ON {table}(name, ts)",
)

_INSERT_SQL = """\
INSERT INTO {table}
    (ts, emitted_at, service, name, event_id, event_ids, sources,
     timestamps, payload_size, payload_sha, payload, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def payload_digest(
    data: BaseModel | dict[str, Any] | None,
) -> tuple[int, str | None, str | None]:
    """返回 `(字节数, sha256, 正文)`；`data` 为空时三者分别是 `0, None, None`。

    `sha256` 而不是原文，是为了让审计库能回答"两次调用的入参是否相同"，同时不必
    承担 `tool.result` 这类大负载的存储成本。
    """
    raw = _serialize(data)
    if raw is None:
        return 0, None, None
    body = raw.encode("utf-8")
    return len(body), hashlib.sha256(body).hexdigest(), raw


def _serialize(data: BaseModel | dict[str, Any] | None) -> str | None:
    """与库侧 `serialize_data` 等价的最小实现，避免依赖 `templates` 的私有导出。"""
    if data is None:
        return None
    if isinstance(data, BaseModel):
        return data.model_dump_json()
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except TypeError, ValueError:
        return str(data)


class JournalMiddleware(Middleware):
    """把本进程发射的每个事件写入 SQLite，并按保留策略自动回收。

    参数
    ----
    db_path:
        单服务一个库文件，例如 `.blue/journal/webui.sqlite`。**不要多个进程共用
        一个文件**——SQLite 的单写者锁会让并发的写入方互相拖死。
    service:
        服务名，写入 `service` 列，同时作为日志前缀。
    table:
        表名，默认 `journal`。
    payload_mode:
        `"digest"`（默认）只存 `payload_size` 与 `payload_sha`；`"full"` 存正文
        （仍受 `payload_max_bytes` 限制）。
    payload_allowlist:
        这些事件名即使在 `digest` 模式下也存正文，用于精确定位关键事件。
    payload_max_bytes:
        正文上限，超限只留摘要——避免把图片、长输出灌进审计库。
    retention:
        保留秒数，默认 1 天；`<= 0` 表示不按时间回收。
    max_rows:
        行数上限；`None` 表示不限。超出后从最老的开始删。
    max_bytes:
        库文件软上限；超出后从最老的成批删除直到回落。`None` 表示不限。
    sweep_interval:
        两次 GC 之间的最小间隔（秒），默认 600。
    batch_size / flush_interval:
        攒批行数与最长等待，二者先到先写。
    queue_size:
        内存缓冲队列长度；满了**丢弃日志**并计数，绝不阻塞发布。
    reconnect_backoff:
        写库失败后多久再尝试重连（秒）。
    vacuum:
        删除后是否执行 `PRAGMA incremental_vacuum` 归还磁盘页。SQLite 的 `DELETE`
        本身不缩小文件，关掉它会看到库只增不减。
    fallback:
        彻底写不进库时的降级回调，默认 `logging.warning`。
    """

    def __init__(
        self,
        db_path: str | Path,
        service: str,
        *,
        table: str = "journal",
        payload_mode: Literal["digest", "full"] = "digest",
        payload_allowlist: Sequence[str] = (),
        payload_max_bytes: int = 8192,
        retention: float = 86400.0,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        sweep_interval: float = 600.0,
        batch_size: int = 128,
        flush_interval: float = 0.5,
        queue_size: int = 8192,
        reconnect_backoff: float = 5.0,
        vacuum: bool = True,
        fallback: Any = None,
    ) -> None:
        if payload_mode not in ("digest", "full"):
            raise ValueError(f"payload_mode 只支持 digest / full：{payload_mode!r}")
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正")
        if queue_size <= 0:
            raise ValueError("queue_size 必须为正")
        if flush_interval < 0:
            raise ValueError("flush_interval 不能为负")

        self._db_path = str(db_path)
        self._service = service
        self._table = table
        self._payload_mode: Literal["digest", "full"] = payload_mode
        self._allowlist = frozenset(payload_allowlist)
        self._payload_max_bytes = max(payload_max_bytes, 0)
        self._retention = retention
        self._max_rows = max_rows if (max_rows or 0) > 0 else None
        self._max_bytes = max_bytes if (max_bytes or 0) > 0 else None
        self._sweep_interval = max(sweep_interval, 0.0)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._reconnect_backoff = max(reconnect_backoff, 0.0)
        self._vacuum = vacuum
        self._fallback: Callable[[str], None] = fallback or (
            lambda line: logger.warning("journal fallback: %s", line)
        )

        self._queue: asyncio.Queue[JournalRow] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._conn: aiosqlite.Connection | None = None
        self._ready = False
        self._closing = False
        self._closed = False
        self._next_retry_at = 0.0
        self._last_sweep = 0.0

        self._written = 0
        self._dropped = 0
        self._failed = 0
        self._collected = 0

        self._ddl = _DDL.format(table=table)
        self._indexes = tuple(sql.format(table=table) for sql in _INDEXES)
        self._insert_sql = _INSERT_SQL.format(table=table)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_setup(self, bus: EventBus) -> None:
        """建库建表，启动后台写入任务，并立刻跑一次 GC。"""
        path = Path(self._db_path)
        if str(path.parent) not in ("", "."):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.exception("journal 目录创建失败，降级运行：%s", path.parent)

        self._closed = False
        await self._open_conn()
        self._closing = False
        self._task = asyncio.create_task(
            self._writer_loop(), name=f"journal:{self._service}"
        )
        await self.collect()

    async def on_teardown(self, bus: EventBus) -> None:
        """停止后台任务并关闭连接，尽力把缓冲写完。"""
        self._closing = True
        # 之后 collect()/stats() 不再重开连接：aiosqlite 的连接线程不是守护线程，
        # 重开而不关会让进程在退出阶段挂住。
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=max(self._flush_interval * 4, 2.0))
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            except Exception:
                logger.debug("journal 写入任务退出异常", exc_info=True)
        await self._close_conn()

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    async def before_publish(
        self,
        event_registry: EventRegistry,
        name: str,
        source: str,
        data: dict[str, Any] | BaseModel | None,
        old_event: Event | None,
        next: BeforePublishNext,
    ) -> None:
        """纯透传：记录点放在 :meth:`on_publish`。"""
        await next(event_registry, name, source, data, old_event)

    async def on_publish(self, event: Event, next: OnPublishNext) -> None:
        """事件已交给传输层——记一行"已发射"。"""
        self._enqueue(self._row_from_event(event))
        await next(event)

    async def on_publish_error(
        self,
        error: Exception,
        name: str,
        source: str,
        data: dict[str, Any] | BaseModel | None,
    ) -> None:
        """发布失败——同样落库，这是审计里最该留下的那一行。"""
        self._enqueue(self._row_from_error(error, name, source, data))

    # ------------------------------------------------------------------
    # 对外可调用
    # ------------------------------------------------------------------

    async def collect(self) -> dict[str, int]:
        """执行一次 GC（按 `retention` / `max_rows` / `max_bytes`）。

        返回 `{"deleted": n}`。CLI 或 supervisor 可在空闲时主动调用；后台循环也会
        每 `sweep_interval` 秒自动跑一次。
        """
        conn = await self._ensure_conn()
        if conn is None:
            return {"deleted": 0}
        self._last_sweep = time.monotonic()

        deleted = 0
        try:
            if self._retention > 0:
                deleted += await self._delete(
                    conn,
                    f"DELETE FROM {self._table} WHERE ts < ?",
                    (time.time() - self._retention,),
                )

            if self._max_rows:
                # 未删除 id 的子查询：`LIMIT -1 OFFSET n` = "第 n 条之后的全部"。
                # 用 OFFSET 而不是 `MAX(id) - n`：AUTOINCREMENT 删除后会留空洞，
                # 后者在删过几轮之后会越删越多。
                deleted += await self._delete(
                    conn,
                    f"DELETE FROM {self._table} WHERE id IN ("
                    f"SELECT id FROM {self._table} "
                    f"ORDER BY id DESC LIMIT -1 OFFSET ?)",
                    (self._max_rows,),
                )

            if self._max_bytes:
                deleted += await self._enforce_max_bytes(conn)

            await conn.commit()
        except Exception:
            logger.exception("journal GC 失败：%s", self._db_path)
            await self._close_conn()
            return {"deleted": deleted}

        if deleted:
            await self._incremental_vacuum(conn, deleted)
        self._collected += deleted
        if deleted:
            logger.info("journal GC：%s 删除 %d 行", self._service, deleted)
        return {"deleted": deleted}

    async def close(self) -> None:
        """关闭连接，且**此后不再重开**。

        总线托管时由 :meth:`on_teardown` 自动调用；脱离总线单独使用（CLI 里只做
        `collect()` / `stats()`）时，必须自己调用它——aiosqlite 的连接线程不是
        守护线程，漏关会让进程在退出阶段挂着。
        """
        self._closed = True
        await self._close_conn()

    async def vacuum(self) -> None:
        """整库 `VACUUM`：重写文件并彻底归还磁盘。

        会持有排他锁并产生等于库大小的临时空间，适合停机维护时调用，不要放进
        常规巡检路径。
        """
        conn = await self._ensure_conn()
        if conn is None:
            return
        try:
            await conn.execute("VACUUM")
            await conn.commit()
        except Exception:
            logger.exception("journal VACUUM 失败：%s", self._db_path)

    async def stats(self) -> dict[str, Any]:
        """运行态与库统计，供 `blue status` 展示。"""
        info: dict[str, Any] = {
            "service": self._service,
            "db_path": self._db_path,
            "ready": self._ready,
            "pending": self._queue.qsize(),
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "collected": self._collected,
        }
        conn = await self._ensure_conn()
        if conn is None:
            return info
        try:
            info["rows"] = await self._scalar(
                conn, f"SELECT COUNT(*) FROM {self._table}"
            )
            page_size = await self._pragma(conn, "PRAGMA page_size")
            page_count = await self._pragma(conn, "PRAGMA page_count")
            info["bytes"] = page_count * page_size
            info["oldest_ts"] = await self._scalar(
                conn, f"SELECT MIN(ts) FROM {self._table}"
            )
            info["newest_ts"] = await self._scalar(
                conn, f"SELECT MAX(ts) FROM {self._table}"
            )
        except Exception:
            logger.debug("journal 统计失败", exc_info=True)
        return info

    # ------------------------------------------------------------------
    # 后台写入
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """攒批写入；空闲时按间隔跑 GC。"""
        try:
            while True:
                if self._closing and self._queue.empty():
                    return
                batch = await self._next_batch()
                if batch:
                    await self._write_batch(batch)
                await self._maybe_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 兜底，避免任务静默死亡
            logger.exception("journal 写入循环异常退出：%s", self._service)

    async def _next_batch(self) -> list[JournalRow]:
        """等第一条，然后在 `flush_interval` 内尽量攒满 `batch_size`。"""
        batch: list[JournalRow] = []
        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=self._flush_interval
            )
        except TimeoutError:
            return batch
        batch.append(first)

        deadline = time.monotonic() + self._flush_interval
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(
                    await asyncio.wait_for(self._queue.get(), timeout=remaining)
                )
            except TimeoutError:
                break
        return batch

    async def _write_batch(self, batch: list[JournalRow]) -> None:
        conn = await self._ensure_conn()
        if conn is None:
            self._failed += len(batch)
            self._spill(batch)
            return
        try:
            await conn.executemany(self._insert_sql, batch)
            await conn.commit()
            self._written += len(batch)
        except Exception:
            logger.exception(
                "journal 批量写入失败（%d 行），丢弃：%s", len(batch), self._db_path
            )
            self._failed += len(batch)
            self._spill(batch)
            # 连接可能已损坏：关掉，下个批次按退避重连。
            await self._close_conn()

    def _spill(self, batch: Iterable[JournalRow]) -> None:
        """写不进库时把关键字段交给 fallback，至少留在进程日志里。"""
        for row in batch:
            self._fallback(
                json.dumps(
                    {
                        "service": row[2],
                        "name": row[3],
                        "event_id": row[4],
                        "payload_size": row[8],
                        "error": row[11],
                    },
                    ensure_ascii=False,
                )
            )

    async def _maybe_sweep(self) -> None:
        if self._sweep_interval <= 0:
            return
        if time.monotonic() - self._last_sweep < self._sweep_interval:
            return
        await self.collect()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def _open_conn(self) -> bool:
        try:
            conn = await aiosqlite.connect(self._db_path)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            # 增量 vacuum 需要先开 auto_vacuum；新库在首次建表前设置即生效。
            # 既有库要切换必须整库 VACUUM 一次（见 :meth:`vacuum`）。
            await conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            # -wal 自身也限个大小，否则它会单独膨胀。
            await conn.execute("PRAGMA journal_size_limit=67108864")
            await conn.execute(self._ddl)
            for sql in self._indexes:
                await conn.execute(sql)
            await conn.commit()
            self._conn = conn
            self._ready = True
            self._next_retry_at = 0.0
            logger.info("journal 就绪：%s", self._db_path)
            return True
        except Exception:
            logger.exception("journal 初始化失败，稍后重试：%s", self._db_path)
            self._ready = False
            self._next_retry_at = time.monotonic() + self._reconnect_backoff
            return False

    async def _ensure_conn(self) -> aiosqlite.Connection | None:
        """返回可用连接；已 `close()` 或处于退避窗口内时返回 `None`。"""
        if self._conn is not None and self._ready:
            return self._conn
        if self._closed:
            return None
        if time.monotonic() < self._next_retry_at:
            return None
        if await self._open_conn():
            return self._conn
        return None

    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        self._ready = False
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                logger.debug("journal 关闭连接失败", exc_info=True)

    # ------------------------------------------------------------------
    # 行构造
    # ------------------------------------------------------------------

    def _enqueue(self, row: JournalRow) -> None:
        """非阻塞入队；满了丢记录并计数——绝不阻塞发布。"""
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(
                    "journal 缓冲已满，累计丢弃 %d 条：%s", self._dropped, self._service
                )

    def _row_from_event(self, event: Event) -> JournalRow:
        size, sha, payload = self._payload_fields(event.name, event.data)
        return (
            time.time(),
            event.timestamps[-1].timestamp() if event.timestamps else None,
            self._service,
            event.name,
            event.id,
            json.dumps(event.event_ids, ensure_ascii=False),
            json.dumps(event.sources, ensure_ascii=False),
            json.dumps([t.isoformat() for t in event.timestamps], ensure_ascii=False),
            size,
            sha,
            payload,
            None,
        )

    def _row_from_error(
        self,
        error: Exception,
        name: str,
        source: str,
        data: dict[str, Any] | BaseModel | None,
    ) -> JournalRow:
        size, sha, _ = self._payload_fields(name, data)
        # 失败路径连 Event 实例都没构造出来，用可识别的占位 id 便于 grep。
        return (
            time.time(),
            None,
            self._service,
            name,
            f"!{uuid.uuid4().hex}",
            "[]",
            json.dumps([source], ensure_ascii=False),
            "[]",
            size,
            sha,
            None,
            f"{type(error).__name__}: {error}",
        )

    def _payload_fields(
        self, name: str, data: dict[str, Any] | BaseModel | None
    ) -> tuple[int, str | None, str | None]:
        size, sha, raw = payload_digest(data)
        if raw is None:
            return size, sha, None
        keep = self._payload_mode == "full" or name in self._allowlist
        if not keep or size > self._payload_max_bytes:
            return size, sha, None
        return size, sha, raw

    # ------------------------------------------------------------------
    # GC 细节
    # ------------------------------------------------------------------

    async def _enforce_max_bytes(self, conn: aiosqlite.Connection) -> int:
        """软上限：从最老的成批删除，直到文件回落到阈值以下。"""
        if not self._max_bytes:
            return 0
        page_size = await self._pragma(conn, "PRAGMA page_size")
        if page_size <= 0:
            return 0

        deleted = 0
        chunk = 1000
        while True:
            page_count = await self._pragma(conn, "PRAGMA page_count")
            if page_count * page_size <= self._max_bytes:
                break
            removed = await self._delete(
                conn,
                f"DELETE FROM {self._table} WHERE id IN ("
                f"SELECT id FROM {self._table} ORDER BY id ASC LIMIT ?)",
                (chunk,),
            )
            deleted += removed
            if removed == 0:
                break
            await conn.commit()
            await self._incremental_vacuum(conn, removed)
        return deleted

    async def _delete(
        self, conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]
    ) -> int:
        cur = await conn.execute(sql, params)
        removed = cur.rowcount or 0
        await cur.close()
        return removed

    async def _incremental_vacuum(self, conn: aiosqlite.Connection, pages: int) -> None:
        """按删除量归还磁盘页；不整库重写，代价远低于 `VACUUM`。"""
        if not self._vacuum or pages <= 0:
            return
        try:
            await conn.execute(f"PRAGMA incremental_vacuum({min(pages, 4096)})")
            await conn.commit()
        except Exception:
            logger.debug("incremental_vacuum 失败", exc_info=True)

    async def _pragma(self, conn: aiosqlite.Connection, sql: str) -> int:
        value = await self._scalar(conn, sql)
        try:
            return int(value)
        except TypeError, ValueError:
            return 0

    async def _scalar(
        self, conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()
    ) -> Any:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def failed(self) -> int:
        return self._failed
