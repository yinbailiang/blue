"""停机预算测试：库的超时拆解必须对得上，且落在 supervisor 的 ``stop_timeout`` 内。

背景（两个坑，都在这里钉住）：

1. ``infinity_bus`` 的默认 ``ShutdownConfig`` 与 supervisor 的停止预算毫无关系
   ——队列空时 ``queue.join()`` 只等 1 秒、最长 15 秒，还随积压条数缩水；
2. 「排空」不是同一件事：``queue.join()`` 等的是消息**派发**完（``task_done()``
   在 ``_dispatch_loop`` 里派发后就调，也就是 ack 发生在处理器开始之前），真正等
   处理器收尾的是 ``tasks_timeout``。把预算全给 queue、只给 tasks 留 1 秒，会让在跑
   的处理器被取消，而它的消息早已 ack——事件**不会重投，是静默丢掉**。

所以本文件盯两件事：**预算的大头给 tasks**，以及**总和不超 stop_timeout**。
"""

from __future__ import annotations

import asyncio

from infinity_bus import (
    Event,
    EventBus,
    EventDeclaration,
    EventHandler,
    EventHandlerRegistry,
    EventRegistry,
)
from pydantic import BaseModel

from blue.runtime import drain_budget_for
from blue.transport import (
    QUEUE_DRAIN_MAX,
    MemoryFanout,
    shutdown_config_for,
)


class _Ping(EventDeclaration):
    """测试用事件声明。"""

    name = "test.shutdown_budget.ping"
    payload_type = None


class _SlowHandler(EventHandler):
    """跑得比旧版 ``tasks_timeout=1s`` 慢的处理器，记录自己是否被砍。"""

    def __init__(self, delay: float) -> None:
        super().__init__(subscriptions=[_Ping.name])
        self.delay = delay
        self.finished = False
        self.cancelled = False

    async def handle(
        self, payload: BaseModel | None, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> None:
        """睡一会儿；被取消时记下来再往上抛（别吞掉 CancelledError）。"""
        try:
            await asyncio.sleep(self.delay)
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _stop_while_handler_runs(delay: float, stop_timeout: float) -> _SlowHandler:
    """处理器在跑的时候调 ``bus.stop()``，返回处理器的结局。"""
    hub = MemoryFanout()
    queue = hub.attach("budget.member")
    events = EventRegistry()
    events.register(_Ping)
    handlers = EventHandlerRegistry()
    handler = _SlowHandler(delay)
    bus = EventBus(
        events,
        handlers,
        queue=queue,
        shutdown=shutdown_config_for(drain_budget_for(stop_timeout)),
    )
    await bus.start()
    try:
        await handlers.register(handler)
        await bus.proxy("test").publish(_Ping.name, data=None)
        await asyncio.sleep(0.2)  # 让处理器进入 sleep
    finally:
        await bus.stop()
    return handler


def test_running_handler_survives_shutdown() -> None:
    """预算 8 秒、处理器要 1.6 秒：必须跑完，不能被 1 秒的 tasks_timeout 砍掉。

    这条正是上一版的回归点：整份预算给了 ``queue.join()``，``tasks_timeout`` 固定
    1.0，于是任何超过 1 秒的处理器（agent 的一轮对话就是几十秒）都会被取消。
    """
    handler = asyncio.run(_stop_while_handler_runs(delay=1.6, stop_timeout=10.0))

    assert handler.finished, "处理器应当跑完：停机预算有 8 秒，不该在 1 秒时被取消"
    assert not handler.cancelled


def test_tasks_timeout_gets_the_bulk_of_budget() -> None:
    """大头给 ``tasks_timeout``：``join()`` 只等「事件全部启动」，等收尾的是它。

    6.1 起 ``task_done()``（= ack）在该事件**所有处理器任务都获得执行权**之后就发，
    所以 ``queue.join()`` 与处理器跑多久无关，只需一个零头；真正等处理器收尾的是
    ``tasks_timeout``。这条方向跟着库的 ack 语义走（见 ``shutdown_config_for`` 注释）。
    """
    config = shutdown_config_for(8.0)

    assert config.tasks_timeout > config.queue_timeout_max
    assert config.queue_timeout_max <= QUEUE_DRAIN_MAX


def test_shutdown_config_uses_fixed_budget() -> None:
    """不管积压多长都给固定预算：``avg_wait_time=0`` 让超时不随 qsize 缩水。"""
    config = shutdown_config_for(8.0)

    assert config.queue_timeout_min == config.queue_timeout_max
    assert config.avg_wait_time == 0.0
    # 库里的算法是 max(min_, min(max_, qsize * avg))；avg=0 时恒等于 min_
    assert max(config.queue_timeout_min, min(config.queue_timeout_max, 999 * 0.0)) == (
        config.queue_timeout_min
    )


def test_budget_fits_inside_stop_timeout() -> None:
    """核心不变量：排空 + 兜底必须装进 supervisor 的 ``stop_timeout``。

    超了就意味着 supervisor 会先 SIGKILL —— 那时已投递未派发的消息必然被重投。

    特意包含 1.0/2.0/3.0 这些小值：旧实现写的是 ``max(1.0, stop_timeout - 2.0)``，
    ``stop_timeout`` 小的时候会被那个下限兜回来，直接破掉这条不变量。
    """
    for stop_timeout in (1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0):
        budget = drain_budget_for(stop_timeout)
        config = shutdown_config_for(budget)
        worst = config.queue_timeout_max + config.tasks_timeout

        assert budget < stop_timeout, (
            f"stop_timeout={stop_timeout}s 时排空预算 {budget}s 没留退出余量"
        )
        assert worst <= budget + 1e-9, (
            f"stop_timeout={stop_timeout}s 装不下 {worst}s 的排空预算"
        )


def test_drain_budget_keeps_a_margin_for_large_timeouts() -> None:
    """大预算固定留 2 秒给进程退出、日志刷盘。"""
    assert drain_budget_for(10.0) == 8.0
    assert drain_budget_for(30.0) == 28.0
