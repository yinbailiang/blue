"""事件去重中间件测试。

盯三条语义：

1. 已处理的 (事件, 处理器) 再投时**被阻断**——这是去重的目的；
2. 处理器**失败不标记**——否则 at-least-once 的重试语义就没了；
3. 同一事件被多个处理器订阅时**逐个判定**——A 成功不该连累 B 被跳过。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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

from blue.idempotency import IdempotencyMiddleware


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
