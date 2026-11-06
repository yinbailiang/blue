"""事件去重中间件：把「幂等」从业务处理器里抽出来。

at-least-once 语义下（未 ack 断连重投 / ``restart`` 补投）事件可能被重复投递。事件
自带唯一 ``Event.id``（uuid4 hex）作去重键。

这里用**中间件**而不是库里的 :class:`~infinity_bus.templates.IdempotentHandler`，
原因有三：

1. **业务零改动**——不用让每个 handler 改成继承幂等基类；
2. **去重键必须是稳定的**——库的 ``IdempotentHandler`` 由调用方注入 ``consumer``，
   如果拿 ``handler_id`` 当键会踩坑：``EventHandlerRegistry.register`` 每次注册都
   生成新的 ``uuid4().hex``，重启后去重表全对不上。这里改用**处理器类名**（稳定）；
3. 记录时机与 ``IdempotentHandler`` 保持一致：**成功才标记**。失败 / 取消 / 停机
   跳过都不标记，留给 at-least-once 重投重试——这正好回答了「异常怎么办」。

存储用的是本模块的 :class:`SqliteDedupeRecorder`（不是库里的
``SqliteIdempotencyRecorder``），原因见它自己的 docstring：库版本每条都 fsync、
且没有清理策略，实测给派发路径加了 13ms/事件、一天涨 5 万行。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from infinity_bus import (
    BeforePublishNext,
    DispatchNext,
    Event,
    EventHandlerRegistry,
    EventRegistry,
    Middleware,
    OnPublishNext,
    TaskRecord,
)
from infinity_bus.templates import IdempotencyRecorder
from pydantic import BaseModel

__all__ = ["IdempotencyMiddleware", "SqliteDedupeRecorder"]

DEFAULT_RETENTION = 7 * 24 * 3600.0
"""去重记录默认保留 7 天——远大于任何重投窗口，同时给磁盘一个上界。"""

DEFAULT_SWEEP_INTERVAL = 600.0
"""清理巡检间隔（秒）：每这么久最多清一次，不把扫表摊到每条事件上。"""


class SqliteDedupeRecorder:
    """SQLite 去重表：跨进程/跨重启，带保留期清理与 WAL。

    为什么不用库里的 ``SqliteIdempotencyRecorder``：

    1. **写放大**：它每条 ``mark_processed`` 都 commit，默认 rollback journal +
       ``synchronous=FULL``，实测单条 6.5ms；去重是「每事件 × 每处理器」各一次，
       等于在派发路径上插了个 fsync。换 ``journal_mode=WAL`` +
       ``synchronous=NORMAL`` 后同一操作 0.02ms（差三个数量级）。
    2. **只增不减**：它没有清理策略，实测 webui 一天 5.2 万行 / 9.9MB。去重的有效
       窗口只是「重投可能发生的那段时间」（几分钟到几小时），留一周是上限而非需要。

    ``synchronous=NORMAL`` 在 WAL 下意味着掉电可能丢最后几个事务。对去重表这是
    **可接受的降级**：少一条记录最坏等于「该事件被重复执行一次」，也就是没有去重
    时的行为；而丢记录不会造成错误结果（处理器本身仍是幂等安全的那一侧）。

    表结构与 ``ts`` 格式与库版本完全一致（``processed_log(consumer, event_id, ts)``，
    ``ts`` 为 UTC ISO 8601），所以库里已有的去重文件可以直接接着用。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        retention: float = DEFAULT_RETENTION,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        """指定去重库路径、保留期与清理巡检间隔。

        :param db_path: SQLite 文件路径（``:memory:`` 也可，用于测试）
        :param retention: 记录保留秒数；超出即删除
        :param sweep_interval: 两次清理之间的最小间隔（秒）
        """
        self._db_path = str(db_path)
        self._retention = retention
        self._sweep_interval = sweep_interval
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._last_sweep = 0.0

    async def start(self) -> None:
        """建连接、开 WAL、建表，并做一次清理（幂等）。"""
        async with self._lock:
            if self._conn is not None:
                return
            self._conn = await asyncio.to_thread(self._open)
            await asyncio.to_thread(self._sweep, self._require_conn(), time.monotonic())
            self._last_sweep = time.monotonic()

    def _open(self) -> sqlite3.Connection:
        """建连接并设置本连接/本库的持久化参数。"""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        # journal_mode 落在库文件上（持久），synchronous 是连接级、必须每条连接设
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_log ("
            " consumer TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " ts TEXT NOT NULL,"
            " PRIMARY KEY (consumer, event_id))"
        )
        conn.commit()
        return conn

    async def is_processed(self, consumer: str, event_id: str) -> bool:
        """``(consumer, event_id)`` 是否已处理。"""
        async with self._lock:
            conn = self._require_conn()
            return await asyncio.to_thread(self._select, conn, consumer, event_id)

    @staticmethod
    def _select(conn: sqlite3.Connection, consumer: str, event_id: str) -> bool:
        cursor = conn.execute(
            "SELECT 1 FROM processed_log WHERE consumer=? AND event_id=?",
            (consumer, event_id),
        )
        return cursor.fetchone() is not None

    async def mark_processed(self, consumer: str, event_id: str) -> None:
        """标记已处理（重复标记安全），并按巡检间隔顺带清理过期记录。"""
        async with self._lock:
            conn = self._require_conn()
            await asyncio.to_thread(self._insert, conn, consumer, event_id)
            now = time.monotonic()
            if self._retention > 0 and now - self._last_sweep >= self._sweep_interval:
                await asyncio.to_thread(self._sweep, conn, now)
                self._last_sweep = now

    @staticmethod
    def _insert(conn: sqlite3.Connection, consumer: str, event_id: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO processed_log (consumer, event_id, ts)"
            " VALUES (?, ?, ?)",
            (consumer, event_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()

    def _sweep(self, conn: sqlite3.Connection, now: float) -> None:
        """删掉超过保留期的记录（``ts`` 是 ISO 8601 UTC，字典序即时间序）。"""
        if self._retention <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(seconds=self._retention)).isoformat()
        conn.execute("DELETE FROM processed_log WHERE ts < ?", (cutoff,))
        conn.commit()

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        async with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                await asyncio.to_thread(conn.close)

    def _require_conn(self) -> sqlite3.Connection:
        """取已建立的连接。

        :raises RuntimeError: 还没 ``await start()``
        """
        if self._conn is None:
            raise RuntimeError("去重记录器尚未 start()")
        return self._conn


class IdempotencyMiddleware(Middleware):
    """按「消费方 + 处理器」维度丢弃重复投递的事件。

    去重粒度是 ``(consumer, 处理器类名, event.id)``：

    - ``consumer`` 为总线成员 ID（``member_id``）——fanout 广播下各成员各自记录，
      互不串扰；
    - 带上处理器类名，是因为一个事件可能被同一成员的多个处理器订阅：A 成功、B 失败
      时，重投只该补跑 B，而不是把 A 再跑一遍。
    """

    def __init__(
        self,
        recorder: IdempotencyRecorder,
        *,
        consumer: str,
        handlers: EventHandlerRegistry,
    ) -> None:
        self._recorder = recorder
        self._consumer = consumer
        self._handlers = handlers

    async def before_publish(
        self,
        event_registry: EventRegistry,
        name: str,
        source: str,
        data: dict[str, Any] | BaseModel | None,
        old_event: Event | None,
        next: BeforePublishNext,
    ) -> None:
        """本中间件不参与发布链路，原样放行。"""
        await next(event_registry, name, source, data, old_event)

    async def on_publish(self, event: Event, next: OnPublishNext) -> None:
        """本中间件不参与发布链路，原样放行。"""
        await next(event)

    async def before_dispatch(
        self,
        event: Event,
        handler_ids: list[str],
        next: DispatchNext,
    ) -> None:
        """把「已处理过」的处理器从本次派发里剔掉。

        只放行没处理过的；全部处理过就不调 ``next``——事件至此终止，重复投递被丢弃。
        """
        remaining: list[str] = []
        for handler_id in handler_ids:
            if not await self._recorder.is_processed(self._key(handler_id), event.id):
                remaining.append(handler_id)
        if remaining:
            await next(event, remaining)

    async def on_task_done(self, record: TaskRecord) -> None:
        """只有真正成功才记账。

        失败（``error``）/ 取消（``cancelled``）/ 停机跳过（``skipped``）都不标记，
        这样 broker 重投时还会再跑一次——重试语义由此保留。

        钩子异常会被总线逐中间件隔离记录，不影响任务结果。
        """
        if record.outcome != "success":
            return
        await self._recorder.mark_processed(
            self._key(record.handler_id, record.handler_name), record.event.id
        )

    def _key(self, handler_id: str, handler_name: str | None = None) -> str:
        """去重键：``消费方:处理器类名``。"""
        name = handler_name if handler_name is not None else self._name_of(handler_id)
        return f"{self._consumer}:{name}"

    def _name_of(self, handler_id: str) -> str:
        """从注册表反查处理器类名；查不到就退回去重键本身（此时仅内存内有效）。"""
        handler = self._handlers.get(handler_id)
        if handler is None:
            return handler_id
        return handler.__class__.__name__
