"""事件去重中间件测试：**处理器获得执行权时的一次性认领**。

盯五条语义：

1. 同一 ``(消费方, 处理器, 事件)`` 只被**执行一次**——重复投递在
   ``before_dispatch`` 被挡掉，并把空列表交给核心层 ack；
2. 认领**只在处理器启动时**落库——派发后、启动前崩溃不会留下记录，重投照常派发；
3. 处理器失败不影响认领——认领不因失败/取消而撤回（失败重试是另一回事）；
4. 粒度是 **per-handler**：一个事件被多个处理器订阅时各自独立认领，
   A 有认领不该连累 B；
5. 停机**放弃派发**时只清内存跟踪：没起来的处理器没有认领，重投后只补跑它们。
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
    EventQueue,
    TaskRecord,
)
from infinity_bus.templates import (
    IdempotencyRecorder,
    InMemoryIdempotencyRecorder,
    SqliteIdempotencyRecorder,
)

from blue.idempotency import (
    IdempotencyMiddleware,
    consumer_key,
    handler_dedupe_key,
    vacuum_databases,
)
from blue.runtime import InfraConfig, idempotency_recorder


class _Tick(EventDeclaration):
    """测试事件。"""

    name = "test.tick"


class _Counter(EventHandler):
    """数一下自己被派发了几次。"""

    def __init__(self, started: asyncio.Event | None = None) -> None:
        super().__init__([_Tick.name], handle_timeout=5.0)
        self.calls = 0
        self.started = started

    async def handle(
        self, payload: object, bus_proxy: object, raw_event: Event
    ) -> None:
        if self.started is not None:
            self.started.set()
        self.calls += 1


class _Other(EventHandler):
    """第二个处理器：同一事件、不同类，验证 per-handler 粒度。"""

    def __init__(self) -> None:
        super().__init__([_Tick.name], handle_timeout=5.0)
        self.calls = 0

    async def handle(
        self, payload: object, bus_proxy: object, raw_event: Event
    ) -> None:
        self.calls += 1


def _record(handler_id: str, handler_name: str, event: Event) -> TaskRecord:
    """构造一条任务记录（``on_task_start`` / ``on_task_done`` 的载荷）。

    ``dispatched_at`` / ``started_at`` 是必填字段（信号量排队时长由两者之差推导），
    这里给同一时刻——本测试不关心排队时长。
    """
    now = datetime.now(UTC)
    return TaskRecord(
        handler_id=handler_id,
        handler_name=handler_name,
        event=event,
        dispatched_at=now,
        started_at=now,
    )


def _middleware(
    recorder: IdempotencyRecorder,
    handlers: EventHandlerRegistry,
    consumer: str = "member.one",
) -> IdempotencyMiddleware:
    return IdempotencyMiddleware(recorder, consumer=consumer, handlers=handlers)


def test_second_delivery_is_acked_but_not_dispatched() -> None:
    """同一事件投两次：第一次派发，处理器启动后记账，第二次被挡掉并交给核心层 ack。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler = _Counter()
        handler_id = await handlers.register(handler)
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        passed: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            passed.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        await middleware.on_task_start(_record(handler_id, "_Counter", event))
        await middleware.before_dispatch(event, [handler_id], proceed)

        assert passed == [[handler_id], []], (
            "第二次必须把空列表交给核心层——不 ack 会在持久队列上留下毒消息"
        )

    asyncio.run(run())


def test_inflight_duplicate_is_not_acked_until_original_starts() -> None:
    """原任务还在排队等执行权时，重复保持未 ack；启动后再投只 ack。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler_id = await handlers.register(_Counter())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        passed: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            passed.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        # 原任务还没启动就收到同一事件（连接抖动后 broker 重投）
        await middleware.before_dispatch(event, [handler_id], proceed)
        assert passed == [[handler_id]], (
            "原任务在飞时不能 ack 重复——原任务若被放弃，事件还得靠它重投"
        )

        await middleware.on_task_start(_record(handler_id, "_Counter", event))
        await middleware.before_dispatch(event, [handler_id], proceed)
        assert passed == [[handler_id], []], "已认领的重复要 ack，不再派发"

    asyncio.run(run())


def test_failed_handler_keeps_its_claim() -> None:
    """处理器失败不影响认领：认领记的是「执行过」，不是「执行成功」。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        handler_id = await handlers.register(_Counter())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)
        key = _key_of(handlers, handler_id, "member.one")

        passed: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            passed.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        # 任务启动即认领；此后以 error 结束（这就是「失败」在本层的全部信息）
        await middleware.on_task_start(_record(handler_id, "_Counter", event))
        await middleware.on_dispatch_abandoned(event)  # 没有未启动的 → 不该撤回

        assert passed == [[handler_id]]
        assert await recorder.is_processed(key, event.id) is True, (
            "已启动的事件不该因为处理器失败而撤回认领（重试是另一套机制）"
        )

        # 再投一次：只 ack，不再派发
        passed.clear()
        await middleware.before_dispatch(event, [handler_id], proceed)
        assert passed == [[]], "失败不撤回认领 ⇒ 重投只 ack，不再派发"

    asyncio.run(run())


class _BrokenRecorder:
    """碰一下就炸的记录器（模拟 SQLite 锁表 / 磁盘故障）。"""

    async def is_processed(self, consumer: str, event_id: str) -> bool:
        raise sqlite3.OperationalError("database is locked")

    async def mark_processed(self, consumer: str, event_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    async def claim(self, consumer: str, event_id: str) -> bool:
        raise sqlite3.OperationalError("database is locked")

    async def release(self, consumer: str, event_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")


def test_dedupe_store_failure_fails_open() -> None:
    """去重库炸了必须放行：查询失败当没执行过，认领写失败只记日志。"""

    async def run() -> None:
        handlers = EventHandlerRegistry()
        handler_id = await handlers.register(_Counter())
        middleware = IdempotencyMiddleware(
            _BrokenRecorder(), consumer="member.one", handlers=handlers
        )
        event = Event(name=_Tick.name)

        passed: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            passed.append(ids)

        await middleware.before_dispatch(event, [handler_id], proceed)
        assert passed == [[handler_id]], "查询失败要放行（at-least-once），不能丢事件"

        # 认领写失败只记日志：跟踪状态清掉，任务照常执行，后续重复也不会卡在这里
        await middleware.on_task_start(_record(handler_id, "_Counter", event))
        assert middleware.tracked_events == 0

    asyncio.run(run())


def test_only_unclaimed_handlers_are_dispatched() -> None:
    """per-handler 粒度：A 已有认领，同一事件的 B 仍应被派发。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        first_id = await handlers.register(_Counter())
        second_id = await handlers.register(_Other())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        # 先单独认领 A（模拟上一次投递只派发了 A）
        assert (
            await recorder.claim(_key_of(handlers, first_id, "member.one"), event.id)
            is True
        )

        dispatched: list[list[str]] = []

        async def proceed(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware.before_dispatch(event, [first_id, second_id], proceed)

        assert dispatched == [[second_id]], "A 已认领过，只该派发 B"

    asyncio.run(run())


def test_crash_before_start_does_not_wedge_redelivery(tmp_path: Path) -> None:
    """派发后、启动前进程没了：库里没有认领，重投必须照常派发。

    这是 P1 的回归哨兵：认领若写在 ``before_dispatch``，SIGKILL（supervisor 的
    ``stop_timeout`` 到点就是这么干的）会留下一条永远匹配得上的记录，重投回来的
    事件被自己的认领挡住——既不执行、也不会 ack。
    """
    db = tmp_path / "dedupe.sqlite"

    async def run() -> None:
        event = Event(name=_Tick.name)

        # 进程 1：派发之后立刻崩溃，on_task_start 从未发生
        handlers_a = EventHandlerRegistry()
        first_id = await handlers_a.register(_Counter())
        first = SqliteIdempotencyRecorder(str(db))
        await first.start()
        try:
            middleware = _middleware(first, handlers_a)

            async def proceed(_event: Event, _ids: list[str]) -> None:
                pass

            await middleware.before_dispatch(event, [first_id], proceed)
        finally:
            await first.close()

        # 进程 2：broker 重投同一条事件（同 id）
        handlers_b = EventHandlerRegistry()
        second_id = await handlers_b.register(_Counter())
        second = SqliteIdempotencyRecorder(str(db))
        await second.start()
        try:
            middleware = _middleware(second, handlers_b)
            dispatched: list[list[str]] = []

            async def proceed_again(_event: Event, ids: list[str]) -> None:
                dispatched.append(ids)

            await middleware.before_dispatch(event, [second_id], proceed_again)
            assert dispatched == [[second_id]], "崩溃窗口里的事件必须能重投重跑"
        finally:
            await second.close()

    asyncio.run(run())


def test_abandoned_dispatch_leaves_unstarted_handlers_unclaimed() -> None:
    """停机放弃派发：已启动的有认领，没起来的根本没有 → 重投只补跑没起来的那个。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        started_id = await handlers.register(_Counter())
        queued_id = await handlers.register(_Other())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        async def proceed(_event: Event, ids: list[str]) -> None:
            assert len(ids) == 2

        await middleware.before_dispatch(event, [started_id, queued_id], proceed)
        # 只有第一个真正获得了执行权；第二个堵在信号量上，停机时被放弃
        await middleware.on_task_start(_record(started_id, "_Counter", event))
        await middleware.on_dispatch_abandoned(event)

        assert await recorder.is_processed(
            _key_of(handlers, started_id, "member.one"), event.id
        ), "已开始的处理器要保留认领（重投不该再跑一遍）"
        assert not await recorder.is_processed(
            _key_of(handlers, queued_id, "member.one"), event.id
        ), "从未开始的处理器不该有认领（认领只在启动时写）"

        # 重投：只补跑没起来的那个
        dispatched: list[list[str]] = []

        async def proceed_again(_event: Event, ids: list[str]) -> None:
            dispatched.append(ids)

        await middleware.before_dispatch(event, [started_id, queued_id], proceed_again)
        assert dispatched == [[queued_id]], "重投只该补跑没起来的处理器"

    asyncio.run(run())


def test_state_is_dropped_once_all_handlers_started() -> None:
    """整条事件都启动后清掉跟踪状态（不留内存垃圾）。"""

    async def run() -> None:
        recorder = InMemoryIdempotencyRecorder()
        handlers = EventHandlerRegistry()
        first_id = await handlers.register(_Counter())
        second_id = await handlers.register(_Other())
        middleware = _middleware(recorder, handlers)
        event = Event(name=_Tick.name)

        async def proceed(_event: Event, ids: list[str]) -> None:
            assert len(ids) == 2

        await middleware.before_dispatch(event, [first_id, second_id], proceed)
        assert middleware.tracked_events == 1, "派发后、启动前应当被跟踪"
        await middleware.on_task_start(_record(first_id, "_Counter", event))
        assert middleware.tracked_events == 1, "还有一个处理器没启动，跟踪不能清"
        await middleware.on_task_start(_record(second_id, "_Other", event))

        assert middleware.tracked_events == 0, "整条事件都启动后不该再留跟踪状态"

        # 全部启动后即便再来一次「放弃」通知，也不该撤回任何认领
        await middleware.on_dispatch_abandoned(event)
        assert await recorder.is_processed(
            _key_of(handlers, first_id, "member.one"), event.id
        )

    asyncio.run(run())


def test_handler_dedupe_key_is_stable_and_overridable() -> None:
    """去重键不依赖注册表 id（每次注册都会变），并允许处理器显式覆盖。"""

    class _Explicit(EventHandler):
        dedupe_key = "explicit:key"

        def __init__(self) -> None:
            super().__init__([_Tick.name], handle_timeout=1.0)

        async def handle(
            self, payload: object, bus_proxy: object, raw_event: Event
        ) -> None:
            pass

    assert handler_dedupe_key(_Counter()) == f"{__name__}._Counter"
    assert handler_dedupe_key(_Counter()) == handler_dedupe_key(_Counter())
    assert handler_dedupe_key(_Explicit()) == "explicit:key"


def test_registry_ids_do_not_leak_into_dedupe_keys() -> None:
    """重启后注册表 id 会变，但去重键不能跟着变——否则去重全失效。"""

    async def run() -> None:
        handlers = EventHandlerRegistry()
        first = await handlers.register(_Counter())

        other = EventHandlerRegistry()
        second = await other.register(_Counter())

        first_handler = handlers.get(first)
        second_handler = other.get(second)
        assert first_handler is not None and second_handler is not None

        assert first != second, "注册表 id 本来就会变"
        assert handler_dedupe_key(first_handler) == handler_dedupe_key(second_handler)

    asyncio.run(run())


def _key_of(handlers: EventHandlerRegistry, handler_id: str, consumer: str) -> str:
    """拼出中间件用的认领键（走被测实现的公开帮手，避免测试复制格式）。"""
    handler = handlers.get(handler_id)
    assert handler is not None
    return consumer_key(consumer, handler)


def _seed_row(path: Path, consumer: str, event_id: str, ts: str) -> None:
    """按库的表结构手工种一条记录（用于验证保留期真的接上了）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def test_idempotency_recorder_uses_infra_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 ``infra`` 配置造记录器：库路径、保留期都真的接上去了。

    存储本身由库提供（自带 WAL + 保留期清理），这里只验接线：种一条 30 天前的记录，
    ``start()`` 后应当被「保留期 = 1 天」清掉。
    """

    async def run() -> None:
        infra = InfraConfig(
            project_root=tmp_path,
            bus_url="amqp://guest:guest@localhost/",
            state_dir=tmp_path / ".blue",
            idempotency_retention=86400.0,
            idempotency_sweep_interval=56.0,
        )
        monkeypatch.setattr("blue.runtime.load_infra_config", lambda: infra)
        monkeypatch.setattr("blue.runtime.service_name", lambda: "demo")
        db = tmp_path / ".blue" / "dedupe" / "demo.sqlite"
        ancient = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        _seed_row(db, "member", "ancient", ancient)

        recorder = idempotency_recorder()

        assert isinstance(recorder, SqliteIdempotencyRecorder), "应当复用库的记录器"
        assert db.exists(), "去重库必须落在 state_dir/dedupe/<service>.sqlite"
        await recorder.start()
        try:
            assert await recorder.is_processed("member", "ancient") is False, (
                "保留期没接上：30 天前的记录还在"
            )
        finally:
            await recorder.close()

    asyncio.run(run())


def test_idempotency_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``supervisor.idempotency = false`` 时不下发记录器。

    排查「某条事件没被处理」时，先确认是不是去重把它吃掉了——关掉开关即可对照。
    """
    infra = InfraConfig(
        project_root=tmp_path,
        bus_url="amqp://guest:guest@localhost/",
        state_dir=tmp_path / ".blue",
        idempotency=False,
    )
    monkeypatch.setattr("blue.runtime.load_infra_config", lambda: infra)

    assert idempotency_recorder() is None


def test_claim_survives_restart_through_middleware(tmp_path: Path) -> None:
    """跨重启去重：第二次「进程」用同一个库，重投的同一事件必须被挡掉。"""
    db = tmp_path / "dedupe.sqlite"

    async def run() -> None:
        event = Event(name=_Tick.name)

        first_handlers = EventHandlerRegistry()
        first_id = await first_handlers.register(_Counter())
        first = SqliteIdempotencyRecorder(str(db))
        await first.start()
        try:
            middleware = _middleware(first, first_handlers)
            dispatched: list[list[str]] = []

            async def proceed(_event: Event, ids: list[str]) -> None:
                dispatched.append(ids)

            await middleware.before_dispatch(event, [first_id], proceed)
            # 处理器启动才算执行过——认领在这一刻落库
            await middleware.on_task_start(_record(first_id, "_Counter", event))
            assert dispatched == [[first_id]]
        finally:
            await first.close()

        second_handlers = EventHandlerRegistry()
        second_id = await second_handlers.register(_Counter())
        second = SqliteIdempotencyRecorder(str(db))
        await second.start()
        try:
            middleware = _middleware(second, second_handlers)
            dispatched = []

            async def proceed_again(_event: Event, ids: list[str]) -> None:
                dispatched.append(ids)

            await middleware.before_dispatch(event, [second_id], proceed_again)
            assert dispatched == [[]], "重启后认出已执行过：只 ack，不再派发"
        finally:
            await second.close()

    asyncio.run(run())


class _DuplicateQueue(EventQueue):
    """预置两份**同一条事件**（同 ``id``）的队列 —— 重复投递就长这样。"""

    def __init__(self, event: Event, *, after: asyncio.Event | None = None) -> None:
        self._pending = [event, event]
        self._after = after
        self._arrived = asyncio.Event()
        self.gets = 0
        self.acks = 0

    async def put(self, event: Event) -> None:
        self._pending.append(event)
        self._arrived.set()

    async def get(self) -> Event:
        while not self._pending:
            self._arrived.clear()
            await self._arrived.wait()
        if len(self._pending) == 1 and self._after is not None:
            await self._after.wait()  # 等原任务真的开始，再投重复（模拟真实的重投间隔）
        self.gets += 1
        return self._pending.pop(0)

    def task_done(self, event: Event) -> None:
        self.acks += 1

    def qsize(self) -> int:
        return len(self._pending)

    async def join(self) -> None:
        while self.acks < self.gets:
            await asyncio.sleep(0.01)


def test_bus_dispatch_is_deduped_and_duplicate_is_acked() -> None:
    """接上真总线：同一条事件投两次，处理器只跑一次，两条消息都被 ack。"""

    async def run() -> None:
        from infinity_bus import EventRegistry, MiddlewareChain

        events = EventRegistry()
        events.register(_Tick)
        handlers = EventHandlerRegistry()
        started = asyncio.Event()
        handler = _Counter(started)
        await handlers.register(handler)
        chain = MiddlewareChain()
        await chain.add(_middleware(InMemoryIdempotencyRecorder(), handlers))
        queue = _DuplicateQueue(Event(name=_Tick.name), after=started)
        bus = EventBus(events, handlers, queue=queue, middleware_chain=chain)
        await bus.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while queue.acks < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            await bus.stop()

        assert handler.calls == 1, f"同一事件应只执行一次，实际 {handler.calls} 次"
        assert queue.gets == 2 and queue.acks == 2, (
            "重复投递必须被 ack，否则持久队列会留下毒消息："
            f"gets={queue.gets} acks={queue.acks}"
        )

    asyncio.run(run())


def _churny_database(path: Path) -> None:
    """造一个"删掉了大量行"的库：文件不缩，空闲页留着。"""
    connection = sqlite3.connect(path)
    connection.execute(
        "create table processed_log (consumer text, event_id text, ts real)"
    )
    connection.executemany(
        "insert into processed_log values (?, ?, ?)",
        [(f"c{index % 50}", f"e{index}", float(index)) for index in range(20000)],
    )
    connection.commit()
    connection.execute("delete from processed_log")
    connection.commit()
    connection.close()


def test_vacuum_databases_dry_run_then_shrink(tmp_path: Path) -> None:
    """去重库删行不缩文件：dry-run 只报可回收量，真跑才 VACUUM 缩下来。"""
    directory = tmp_path / "dedupe"
    directory.mkdir()
    path = directory / "demo.sqlite"
    _churny_database(path)
    before = path.stat().st_size

    report = vacuum_databases(tmp_path, dry_run=True)
    assert report.files == 1 and report.reclaimed > 0 and not report.skipped
    assert path.stat().st_size == before

    report = vacuum_databases(tmp_path, dry_run=False)
    assert report.reclaimed > 0
    assert path.stat().st_size < before


def test_vacuum_databases_without_files(tmp_path: Path) -> None:
    """没有去重库时给一个空报告，不报错。"""
    report = vacuum_databases(tmp_path, dry_run=True)

    assert report.files == 0 and report.reclaimed == 0 and not report.skipped
