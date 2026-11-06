"""事件去重中间件测试。

盯三条语义：

1. 已处理的 (事件, 处理器) 再投时**被阻断**——这是去重的目的；
2. 处理器**失败不标记**——否则 at-least-once 的重试语义就没了；
3. 同一事件被多个处理器订阅时**逐个判定**——A 成功不该连累 B 被跳过。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from infinity_bus import (
    Event,
    EventBus,
    EventDeclaration,
    EventHandler,
    EventHandlerRegistry,
    TaskRecord,
)
from infinity_bus.templates import InMemoryIdempotencyRecorder
from pydantic import BaseModel

from blue.idempotency import IdempotencyMiddleware, SqliteDedupeRecorder


class _Tick(EventDeclaration):
    """测试用事件声明。"""

    name = "test.tick"
    payload_type = None


class _Counter(EventHandler):
    """只记调用次数的处理器（真正执行与否由中间件决定）。"""

    def __init__(self) -> None:
        super().__init__(subscriptions=[_Tick.name])

    async def handle(
        self, payload: BaseModel | None, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> None:
        """什么也不做：本测试只关心是否被派发。"""


class _Other(EventHandler):
    """第二个处理器：验证同一事件的多处理器各自判定。"""

    def __init__(self) -> None:
        super().__init__(subscriptions=[_Tick.name])

    async def handle(
        self, payload: BaseModel | None, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> None:
        """什么也不做。"""


def _record(
    handler_id: str, handler_name: str, event: Event, outcome: str
) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        handler_id=handler_id,
        handler_name=handler_name,
        event=event,
        dispatched_at=now,
        started_at=now,
        outcome=outcome,  # type: ignore[arg-type]
    )


def _middleware(
    recorder: InMemoryIdempotencyRecorder, handlers: EventHandlerRegistry
) -> IdempotencyMiddleware:
    return IdempotencyMiddleware(recorder, consumer="member.one", handlers=handlers)


def test_processed_event_is_blocked() -> None:
    """成功处理过之后，同一事件再投会被阻断（不调 next）。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler = _Counter()
        handler_id = await handlers.register(handler)
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        # 第一次：放行
        await middleware.before_dispatch(event, [handler_id], proceed)
        assert dispatched == [[handler_id]]

        # 处理器成功结束 → 记账
        await middleware.on_task_done(_record(handler_id, "_Counter", event, "success"))

        # 第二次（模拟重投）：被阻断
        await middleware.before_dispatch(event, [handler_id], proceed)
        assert dispatched == [[handler_id]], "重复投递不该再次派发"

    asyncio.run(run())


def test_failed_handler_is_not_marked() -> None:
    """失败不记账：重投时还得再跑一次，重试语义不能被去重吃掉。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler_id = await handlers.register(_Counter())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        await middleware.on_task_done(_record(handler_id, "_Counter", event, "error"))

        await middleware.before_dispatch(event, [handler_id], proceed)
        assert len(dispatched) == 2, "失败的处理器必须允许重投重试"

    asyncio.run(run())


def test_only_unfinished_handlers_are_dispatched() -> None:
    """A 成功、B 未完成时，重投只补跑 B。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        first_id = await handlers.register(_Counter())
        second_id = await handlers.register(_Other())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware.before_dispatch(event, [first_id, second_id], proceed)
        assert dispatched[-1] == [first_id, second_id]

        # 只有第一个成功
        await middleware.on_task_done(_record(first_id, "_Counter", event, "success"))

        await middleware.before_dispatch(event, [first_id, second_id], proceed)
        assert dispatched[-1] == [second_id], "已成功的处理器不该被重跑"

    asyncio.run(run())


def test_skipped_outcome_still_allows_retry() -> None:
    """停机跳过（skipped）说明压根没执行，重投必须放行。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler_id = await handlers.register(_Counter())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        await middleware.on_task_done(_record(handler_id, "_Counter", event, "skipped"))
        await middleware.before_dispatch(event, [handler_id], proceed)

        assert len(dispatched) == 2

    asyncio.run(run())


def test_dedupe_key_is_stable_across_handler_ids() -> None:
    """去重键用处理器**类名**，不用 ``handler_id``。

    ``handler_id`` 每次注册都是新的 uuid4，拿它当持久化键会让重启后去重表全失效——
    这条测试就是把这个坑钉死。
    """

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        event = Event(name=_Tick.name)

        # 第一次"进程"：注册 → 处理成功
        handlers_a = EventHandlerRegistry()
        id_a = await handlers_a.register(_Counter())
        middleware_a = _middleware(recorder, handlers_a)
        await middleware_a.on_task_done(_record(id_a, "_Counter", event, "success"))

        # 第二次"进程"：重新注册（handler_id 变了），但类名相同
        handlers_b = EventHandlerRegistry()
        id_b = await handlers_b.register(_Counter())
        assert id_b != id_a, "前提：handler_id 每次注册都不同"
        middleware_b = _middleware(recorder, handlers_b)

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware_b.before_dispatch(event, [id_b], proceed)
        assert dispatched == [], "类名相同就该认出是已处理过的处理器"

    asyncio.run(run())


def _sqlite_rows(path: Path) -> list[tuple[str, str, str]]:
    """直接读去重库，绕开被测代码（校验落库效果，而不是校验它自己的读数）。"""
    connection = sqlite3.connect(path)
    try:
        cursor = connection.execute(
            "SELECT consumer, event_id, ts FROM processed_log ORDER BY event_id"
        )
        return [(str(a), str(b), str(c)) for a, b, c in cursor.fetchall()]
    finally:
        connection.close()


def _insert_row(path: Path, consumer: str, event_id: str, ts: str) -> None:
    """按库版本的 schema/格式手工写一行（模拟历史数据或旧记录）。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS processed_log ("
            " consumer TEXT NOT NULL, event_id TEXT NOT NULL, ts TEXT NOT NULL,"
            " PRIMARY KEY (consumer, event_id))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO processed_log VALUES (?, ?, ?)",
            (consumer, event_id, ts),
        )
        connection.commit()
    finally:
        connection.close()


def test_dedupe_recorder_enables_wal(tmp_path: Path) -> None:
    """开 WAL：默认 rollback journal + ``synchronous=FULL`` 每条都要 fsync。

    实测单条 ``is_processed`` + ``mark_processed`` 在默认参数下 13ms（去重是「每事件
    × 每处理器」各一次，等于给派发路径插了个 fsync），WAL + NORMAL 后 0.02ms。
    """

    async def run() -> None:
        recorder = SqliteDedupeRecorder(tmp_path / "dedupe.sqlite")
        await recorder.start()
        try:
            connection = sqlite3.connect(tmp_path / "dedupe.sqlite")
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()
            finally:
                connection.close()
            assert mode is not None
            assert str(mode[0]).lower() == "wal"
        finally:
            await recorder.close()

    asyncio.run(run())


def test_dedupe_recorder_marks_and_reads(tmp_path: Path) -> None:
    """基本语义：标记过就是已处理，没标记过就不是；重复标记安全。"""

    async def run() -> None:
        recorder = SqliteDedupeRecorder(tmp_path / "dedupe.sqlite")
        await recorder.start()
        try:
            assert await recorder.is_processed("member", "evt-1") is False
            await recorder.mark_processed("member", "evt-1")
            await recorder.mark_processed("member", "evt-1")  # 幂等
            assert await recorder.is_processed("member", "evt-1") is True
            assert await recorder.is_processed("other", "evt-1") is False
        finally:
            await recorder.close()

    asyncio.run(run())


def test_dedupe_recorder_prunes_expired_rows_on_start(tmp_path: Path) -> None:
    """启动时按保留期清一次：库只该留「还在重投窗口内」的记录。

    不清的后果是只增不减——实测 webui 一天 5.2 万行 / 9.9MB。
    """
    db = tmp_path / "dedupe.sqlite"
    now = datetime.now(UTC)
    _insert_row(db, "member", "ancient", (now - timedelta(days=30)).isoformat())
    _insert_row(db, "member", "fresh", (now - timedelta(minutes=5)).isoformat())

    async def run() -> None:
        recorder = SqliteDedupeRecorder(db, retention=7 * 24 * 3600.0)
        await recorder.start()
        await recorder.close()

    asyncio.run(run())

    remaining = [row[1] for row in _sqlite_rows(db)]
    assert remaining == ["fresh"], "过期记录该删、窗口内的该留"


def test_dedupe_recorder_sweeps_while_writing(tmp_path: Path) -> None:
    """长跑进程也要清：按巡检间隔在写路径上顺带清理。"""
    db = tmp_path / "dedupe.sqlite"

    async def run() -> None:
        recorder = SqliteDedupeRecorder(db, retention=60.0, sweep_interval=0.0)
        await recorder.start()
        try:
            _insert_row(db, "member", "ancient", "2000-01-01T00:00:00+00:00")
            await recorder.mark_processed("member", "fresh")
        finally:
            await recorder.close()

    asyncio.run(run())

    remaining = [row[1] for row in _sqlite_rows(db)]
    assert remaining == ["fresh"]


def test_dedupe_recorder_survives_restart(tmp_path: Path) -> None:
    """去重表跟着文件走：换一个进程（新记录器）仍认得出「处理过」。"""
    db = tmp_path / "dedupe.sqlite"

    async def run() -> None:
        first = SqliteDedupeRecorder(db)
        await first.start()
        await first.mark_processed("member", "evt-1")
        await first.close()

        second = SqliteDedupeRecorder(db)
        await second.start()
        try:
            assert await second.is_processed("member", "evt-1") is True
        finally:
            await second.close()

    asyncio.run(run())


def test_dedupe_recorder_reuses_library_table(tmp_path: Path) -> None:
    """沿用库的表结构与 ``ts`` 格式：已有的 ``.blue/dedupe/*.sqlite`` 能直接用。"""
    db = tmp_path / "dedupe.sqlite"
    _insert_row(db, "member", "old-row", datetime.now(UTC).isoformat())

    async def run() -> None:
        recorder = SqliteDedupeRecorder(db)
        await recorder.start()
        try:
            assert await recorder.is_processed("member", "old-row") is True
            await recorder.mark_processed("member", "new-row")
            assert await recorder.is_processed("member", "new-row") is True
        finally:
            await recorder.close()

    asyncio.run(run())

    assert {row[1] for row in _sqlite_rows(db)} == {"old-row", "new-row"}


def test_dedupe_recorder_close_is_idempotent(tmp_path: Path) -> None:
    """重复 ``close()`` 不该炸（停机路径可能走两次）。"""

    async def run() -> None:
        recorder = SqliteDedupeRecorder(tmp_path / "dedupe.sqlite")
        await recorder.start()
        await recorder.close()
        await recorder.close()

    asyncio.run(run())


def test_dedupe_recorder_requires_start(tmp_path: Path) -> None:
    """没 ``start()`` 就用要说清楚，而不是抛 ``AttributeError: NoneType``。"""

    async def run() -> None:
        recorder = SqliteDedupeRecorder(tmp_path / "dedupe.sqlite")
        with pytest.raises(RuntimeError, match="start"):
            await recorder.is_processed("member", "evt-1")

    asyncio.run(run())


def test_dedupe_survives_restart_through_middleware(tmp_path: Path) -> None:
    """跨重启去重：第二次「进程」用同一个库，重投的同一事件必须被丢掉。"""
    db = tmp_path / "dedupe.sqlite"

    async def run() -> None:
        event = Event(name=_Tick.name)

        first_handlers = EventHandlerRegistry()
        first_id = await first_handlers.register(_Counter())
        first = SqliteDedupeRecorder(db)
        await first.start()
        try:
            middleware = IdempotencyMiddleware(
                first, consumer="member.one", handlers=first_handlers
            )
            await middleware.on_task_done(
                _record(first_id, "_Counter", event, "success")
            )
        finally:
            await first.close()

        second_handlers = EventHandlerRegistry()
        second_id = await second_handlers.register(_Counter())
        second = SqliteDedupeRecorder(db)
        await second.start()
        try:
            middleware = IdempotencyMiddleware(
                second, consumer="member.one", handlers=second_handlers
            )
            dispatched: list[list[str]] = []

            async def proceed(_event: Event, ids: list[str]) -> None:
                dispatched.append(ids)

            await middleware.before_dispatch(event, [second_id], proceed)
            assert dispatched == [], "重启后仍应认出这条事件已经处理过"
        finally:
            await second.close()

    asyncio.run(run())
