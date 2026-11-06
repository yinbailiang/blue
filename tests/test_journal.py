"""审计日志中间件测试：**全量记录发射侧事件 + 有界回收**。

盯五条语义：

1. 每个事件**只记一次**（挂在 ``on_publish`` 的发射侧，不是 fanout 的每成员一份）；
2. 因果元数据（``event_id`` / ``event_ids`` / ``sources`` / ``timestamps``）完整落库；
3. 负载默认只存 ``size`` + ``sha256``，白名单与超限各有明确行为；
4. **发布失败也要落库**——审计里最该留下的就是失败那一行；
5. GC 三条策略（保留期 / 行数 / 字节）都真的删得干净，且关库后不再重连。

第 5 条的后半句是回归测试：``aiosqlite`` 的连接线程不是守护线程，
``on_teardown`` 之后再调 ``collect()`` 把连接重开而不关，进程会挂在退出阶段。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

from infinity_bus import (
    EventBus,
    EventDeclaration,
    EventHandlerRegistry,
    EventRegistry,
    MiddlewareChain,
)
from pydantic import BaseModel

from blue.journal import JournalMiddleware
from blue.transport import MemoryFanout


class _Ping(BaseModel):
    text: str


class _PingEvent(EventDeclaration):
    name = "test.journal.ping"
    payload_type = _Ping


class _BigEvent(EventDeclaration):
    name = "test.journal.big"
    payload_type = _Ping


async def _bus(middleware: JournalMiddleware) -> EventBus:
    """建一个单成员的进程内总线，并把日志中间件挂在发射侧。"""
    registry = EventRegistry()
    registry.register(_PingEvent)
    registry.register(_BigEvent)
    chain = MiddlewareChain()
    await chain.add(middleware)
    return EventBus(
        registry,
        EventHandlerRegistry(),
        queue=MemoryFanout().attach("svc"),
        middleware_chain=chain,
    )


def _rows(
    db_path: Path, sql: str, params: tuple[object, ...] = ()
) -> list[tuple[Any, ...]]:
    """直接读库；测试里要断言的是列内容，不是服务 API。"""
    connection = sqlite3.connect(db_path)
    try:
        fetched = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return cast(list[tuple[Any, ...]], fetched)


def _count(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> int:
    return cast(int, _rows(db_path, sql, params)[0][0])


def test_each_published_event_is_recorded_once(tmp_path: Path) -> None:
    """三条事件 → 三行，且因果元数据完整。"""

    async def run() -> None:
        db = tmp_path / "journal" / "svc.sqlite"
        bus = await _bus(JournalMiddleware(db, "svc", flush_interval=0.05))
        async with bus:
            proxy = bus.proxy("svc")
            await proxy.publish("test.journal.ping", _Ping(text="a"))
            await proxy.publish("test.journal.ping", _Ping(text="b"))
            await proxy.publish("test.journal.ping", _Ping(text="c"))
            await asyncio.sleep(0.2)

        rows = _rows(
            db,
            "SELECT service, name, event_id, event_ids, sources, timestamps "
            "FROM journal WHERE name = 'test.journal.ping' ORDER BY id",
        )
        assert len(rows) == 3, f"每个事件应恰好一行，实际 {len(rows)}"
        service, name, event_id, event_ids, sources, timestamps = cast(
            tuple[str, str, str, str, str, str], rows[0]
        )
        assert service == "svc"
        assert name == "test.journal.ping"
        assert len(event_id) == 32, "event_id 应是 uuid4 hex"
        assert event_id in event_ids, "event_ids 必须包含自己这一跳"
        assert sources == '["svc"]', f"sources 应记录发射者，实际 {sources}"
        assert timestamps.startswith("["), "timestamps 应是 ISO 数组"
        assert len({cast(str, row[2]) for row in rows}) == 3, "三行必须是三个事件"

    asyncio.run(run())


def test_payload_defaults_to_digest(tmp_path: Path) -> None:
    """默认模式只存 size + sha256，不存正文。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        bus = await _bus(JournalMiddleware(db, "svc", flush_interval=0.05))
        async with bus:
            await bus.proxy("svc").publish("test.journal.ping", _Ping(text="hello"))
            await asyncio.sleep(0.2)

        size, sha, payload = cast(
            tuple[int, str | None, str | None],
            _rows(
                db,
                "SELECT payload_size, payload_sha, payload FROM journal "
                "WHERE name = 'test.journal.ping'",
            )[0],
        )
        expected = len(_Ping(text="hello").model_dump_json().encode())
        assert size == expected
        assert sha is not None and len(sha) == 64
        assert payload is None, "默认模式不该存正文"

    asyncio.run(run())


def test_payload_allowlist_keeps_body(tmp_path: Path) -> None:
    """白名单里的事件即使在 digest 模式也留正文。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        bus = await _bus(
            JournalMiddleware(
                db,
                "svc",
                flush_interval=0.05,
                payload_allowlist=("test.journal.ping",),
            )
        )
        async with bus:
            await bus.proxy("svc").publish("test.journal.ping", _Ping(text="keep"))
            await asyncio.sleep(0.2)

        payload = _rows(
            db,
            "SELECT payload FROM journal WHERE name = 'test.journal.ping'",
        )[0][0]
        assert payload == '{"text":"keep"}'

    asyncio.run(run())


def test_oversized_payload_falls_back_to_digest(tmp_path: Path) -> None:
    """白名单事件超过 payload_max_bytes 也只留摘要，避免把大负载灌进审计库。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        bus = await _bus(
            JournalMiddleware(
                db,
                "svc",
                flush_interval=0.05,
                payload_allowlist=("test.journal.big",),
                payload_max_bytes=64,
            )
        )
        async with bus:
            await bus.proxy("svc").publish("test.journal.big", _Ping(text="x" * 500))
            await asyncio.sleep(0.2)

        size, sha, payload = cast(
            tuple[int, str | None, str | None],
            _rows(
                db,
                "SELECT payload_size, payload_sha, payload FROM journal "
                "WHERE name = 'test.journal.big'",
            )[0],
        )
        assert size > 64
        assert sha is not None
        assert payload is None

    asyncio.run(run())


def test_publish_error_is_recorded(tmp_path: Path) -> None:
    """发布失败同样落库：只记成功等于默认系统 100% 健康。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        middleware = JournalMiddleware(db, "svc", flush_interval=0.05)
        bus = await _bus(middleware)
        async with bus:
            await middleware.on_publish_error(
                RuntimeError("broker down"),
                "test.journal.ping",
                "svc",
                _Ping(text="lost"),
            )
            await asyncio.sleep(0.2)

        rows = _rows(db, "SELECT event_id, error FROM journal WHERE error IS NOT NULL")
        assert len(rows) == 1
        event_id, error = cast(tuple[str, str], rows[0])
        assert event_id.startswith("!"), "失败行应有可识别的占位 id"
        assert "broker down" in error

    asyncio.run(run())


def test_retention_deletes_expired_rows(tmp_path: Path) -> None:
    """保留期到了就删；新行不受影响。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        middleware = JournalMiddleware(
            db, "svc", flush_interval=0.05, retention=60.0, sweep_interval=0
        )
        bus = await _bus(middleware)
        async with bus:
            await bus.proxy("svc").publish("test.journal.ping", _Ping(text="new"))
            await asyncio.sleep(0.2)
            connection = sqlite3.connect(db)
            connection.execute(
                "INSERT INTO journal (ts, service, name, event_id) VALUES (?,?,?,?)",
                (time.time() - 7200, "svc", "test.journal.ping", "old"),
            )
            connection.commit()
            connection.close()

            assert (await middleware.collect())["deleted"] >= 1

        assert _count(db, "SELECT COUNT(*) FROM journal WHERE event_id='old'") == 0
        assert _count(db, "SELECT COUNT(*) FROM journal WHERE event_id<>'old'") >= 1

    asyncio.run(run())


def test_max_rows_keeps_exactly_n(tmp_path: Path) -> None:
    """行数上限是"恰好 N 行"，不是 N+1。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        middleware = JournalMiddleware(
            db, "svc", flush_interval=0.02, max_rows=3, sweep_interval=0
        )
        bus = await _bus(middleware)
        async with bus:
            proxy = bus.proxy("svc")
            for index in range(8):
                await proxy.publish("test.journal.ping", _Ping(text=str(index)))
            await asyncio.sleep(0.3)
            await middleware.collect()

            assert _count(db, "SELECT COUNT(*) FROM journal") == 3

    asyncio.run(run())


def test_teardown_flushes_buffer(tmp_path: Path) -> None:
    """停机时缓冲必须写完：``flush_interval`` 还没到也不能丢。"""

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        bus = await _bus(JournalMiddleware(db, "svc", flush_interval=0.5))
        async with bus:
            await bus.proxy("svc").publish("test.journal.ping", _Ping(text="last"))

        count = _count(
            db, "SELECT COUNT(*) FROM journal WHERE name='test.journal.ping'"
        )
        assert count == 1

    asyncio.run(run())


def test_close_stops_reconnect(tmp_path: Path) -> None:
    """关库之后 ``collect()`` / ``stats()`` 不得重开连接。

    ``aiosqlite`` 的连接线程不是守护线程——重开而不关会让进程退出时挂住。
    """

    async def run() -> None:
        db = tmp_path / "svc.sqlite"
        middleware = JournalMiddleware(db, "svc", flush_interval=0.05)
        bus = await _bus(middleware)
        async with bus:
            await bus.proxy("svc").publish("test.journal.ping", _Ping(text="x"))
            await asyncio.sleep(0.2)

        assert middleware.is_ready is False, "on_teardown 后应已关连接"
        assert await middleware.collect() == {"deleted": 0}, "已关闭的库不该被重开"
        stats = await middleware.stats()
        assert stats["ready"] is False
        assert "rows" not in stats, "没连接就不该有库统计"
        await middleware.close()

    asyncio.run(run())
