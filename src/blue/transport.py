"""传输运行时：把 ``infinity_bus`` 装配成可跨进程的总线成员。

一个进程 = 一个总线成员（``member_id``）。每个成员持有自己的事件注册表、
处理器注册表与事件队列：

- :class:`RabbitRuntime` —— 生产用；注入 ``RabbitFanoutQueue``，多进程通过
  RabbitMQ fanout 表现为同一个逻辑总线；
- :class:`MemoryRuntime` —— 单进程内的等价实现（测试与本地演示），多个成员
  共享一个 :class:`MemoryFanout` 广播中心。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from infinity_bus import (
    Event,
    EventBus,
    EventDeclaration,
    EventHandler,
    EventHandlerRegistry,
    EventQueue,
    EventRegistry,
    MiddlewareChain,
    ShutdownConfig,
)
from infinity_bus.templates import IdempotencyRecorder, RabbitFanoutQueue

from blue.idempotency import IdempotencyMiddleware

__all__ = [
    "BusRuntime",
    "MemoryFanout",
    "MemoryMemberQueue",
    "MemoryRuntime",
    "RabbitRuntime",
]

logger = logging.getLogger(__name__)


QUEUE_DRAIN_SHARE = 0.25
"""``queue.join()`` 在排空预算里的份额（上限另见 :data:`QUEUE_DRAIN_MAX`）。"""

QUEUE_DRAIN_MAX = 2.0
"""``queue.join()`` 最多占用的秒数；再多也没用（见 :func:`shutdown_config_for`）。"""

SHUTDOWN_INTENT_TTL = 300.0
"""下线意图的有效期（秒）；更旧的文件视为上一次停机的残留，不理会。

意图文件由 supervisor 在 ``down`` 前写、服务停机时读一次即删，正常情况下活不过
一次停机。会留下陈旧文件的情形是「写完意图、没停完就被强杀」——那时按 ``restart``
处理才安全（保留积压），绝不能拿它去切 ``offline``（解绑 + 排空 = 丢积压）。
"""


def shutdown_config_for(drain_budget: float) -> ShutdownConfig:
    """按排空预算推导 ``EventBus`` 的停机配置。

    两个超时管的是**不同的事**，不能按字面"排空"去分配：

    - ``queue_timeout_*`` 限的是 ``EventBus.stop()`` 里的 ``queue.join()``，而库的
      ``task_done()``（= ``basic_ack``）是在 ``_dispatch_loop`` 派发完就调的，所以
      join 等的是「已投递的消息**派发**完」——通常几十毫秒，与处理器快慢无关；
    - ``tasks_timeout`` 才是 ``_wait_all_tasks_done()`` 等**正在跑的处理器**收尾的
      时限。给少了，处理器被取消，而它的消息早在派发时就 ack 了——事件不会被重投，
      是**静默丢掉**。

    所以大头必须给 ``tasks_timeout``：``drain_budget`` 减去 queue 的小零头，剩下的
    全给在跑的任务。上一版把整份预算给了 queue、只给 tasks 留 1 秒，实测让一个 3 秒
    的处理器在停机时被砍（而库默认给 15 秒）。

    ``drain_budget`` 应当已小于 supervisor 的 ``stop_timeout``（见
    :func:`blue.runtime.drain_budget_for`）。
    """
    total = max(0.1, drain_budget)
    queue_share = min(QUEUE_DRAIN_MAX, total * QUEUE_DRAIN_SHARE)
    return ShutdownConfig(
        queue_timeout_min=queue_share,
        queue_timeout_max=queue_share,
        avg_wait_time=0.0,  # 让超时恒等于 queue_share，不随 qsize 缩水
        tasks_timeout=max(0.1, total - queue_share),
    )


@runtime_checkable
class _ManagedRecorder(Protocol):
    """带生命周期的幂等记录器（``SqliteIdempotencyRecorder`` 这类）。

    ``IdempotencyRecorder`` 协议只约定 ``is_processed`` / ``mark_processed``，
    内存版没有开闭钩子——所以这里单独探测，不能无脑调 ``start()``。
    """

    async def start(self) -> None:
        """建立连接 / 建表。"""
        ...

    async def close(self) -> None:
        """释放连接。"""
        ...


class BusRuntime(ABC):
    """总线成员运行时：统一事件注册、处理器注册与生命周期。

    每个成员只注册自己生产/消费的事件声明；不再有全局事件清单。
    """

    def __init__(
        self,
        *,
        member_id: str,
        events: Iterable[type[EventDeclaration]],
        drain_timeout: float | None = None,
        idempotency: IdempotencyRecorder | None = None,
        intent_path: Path | None = None,
    ) -> None:
        if not member_id.strip():
            raise ValueError("member_id 不能为空")
        self._member_id = member_id
        self._events = EventRegistry()
        for event in events:
            self._events.register(event)
        self._handlers = EventHandlerRegistry()
        self._bus: EventBus | None = None
        self._drain_timeout = drain_timeout
        self._idempotency = idempotency
        self._intent_path = intent_path

    def _shutdown_config(self) -> ShutdownConfig:
        """停机超时配置；未指定预算时沿用库默认值。"""
        if self._drain_timeout is None:
            return ShutdownConfig()
        return shutdown_config_for(self._drain_timeout)

    def set_shutdown_strategy(self, strategy: Literal["restart", "offline"]) -> None:
        """切换停机策略；默认实现什么也不做（内存运行时没有队列策略）。"""
        _ = strategy

    def _before_bus_stop(self) -> None:
        """停机前读 supervisor 留下的下线意图，必要时切 ``offline``。

        意图文件是**按服务**一份的（见 ``blue.runtime.shutdown_intent_path``）：读完
        即删不会影响别的服务，而共享一份时只有第一个停机的服务能看到它。

        过期文件（超过 :data:`SHUTDOWN_INTENT_TTL`）直接丢弃：它是上一次停机被强杀
        留下的残骸，拿它切 ``offline`` 会在普通重启里解绑队列、丢掉积压。

        放在基类是因为只有「读文件 + 切策略」这一步——切策略本身由子类实现
        :meth:`set_shutdown_strategy`（内存运行时是 no-op，``intent_path`` 为 None）。
        """
        path = self._intent_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            logger.warning("下线意图无法解析，本次按原策略停机")
            return
        with suppress(OSError):
            path.unlink()  # 一次性：留着会污染下一次普通重启
        if not isinstance(raw, dict):
            return
        data = cast(dict[str, object], raw)
        if data.get("mode") != "offline":
            return
        timestamp = data.get("ts")
        if isinstance(timestamp, int | float):
            age = time.time() - float(timestamp)
            if age > SHUTDOWN_INTENT_TTL:
                logger.warning(
                    "下线意图已过期（%.0f 秒前写下），本次按原策略停机（积压保留）",
                    age,
                )
                return
        logger.info("读到下线意图：本次停机走 offline（解绑并排空，不留积压）")
        self.set_shutdown_strategy("offline")

    @property
    def member_id(self) -> str:
        """本成员在总线上的唯一标识。"""
        return self._member_id

    @property
    def bus(self) -> EventBus:
        """已启动的事件总线。

        :raises RuntimeError: 运行时尚未启动。
        """
        if self._bus is None:
            raise RuntimeError("总线运行时尚未启动，请先 await runtime.start()")
        return self._bus

    @property
    def is_running(self) -> bool:
        """运行时是否已启动。"""
        return self._bus is not None

    def proxy(self, source: str | None = None) -> EventBus.Proxy:
        """创建总线代理；``source`` 缺省为成员 ID。"""
        return self.bus.proxy(source or self._member_id)

    async def register(self, handler: EventHandler) -> str:
        """注册事件处理器（运行时已启动时立即激活订阅）。"""
        return await self._handlers.register(handler)

    async def start(self) -> None:
        """打开队列并启动总线（幂等）。"""
        if self._bus is not None:
            return
        chain = MiddlewareChain()
        if self._idempotency is not None:
            if isinstance(self._idempotency, _ManagedRecorder):
                await self._idempotency.start()
            await chain.add(
                IdempotencyMiddleware(
                    self._idempotency, consumer=self._member_id, handlers=self._handlers
                )
            )
        try:
            queue = await self._open_queue()
            bus = EventBus(
                self._events,
                self._handlers,
                queue=queue,
                shutdown=self._shutdown_config(),
                middleware_chain=chain,
            )
            self._bus = bus
            await bus.start()
        except Exception:
            self._bus = None
            await self._close_queue()
            await self._close_recorder()
            raise

    async def stop(self) -> None:
        """停止总线并释放队列（幂等）。"""
        bus, self._bus = self._bus, None
        try:
            if bus is not None:
                # 必须在 bus.stop() 之前判定：停机策略只在 join() 那一刻被读取
                self._before_bus_stop()
                await bus.stop()
        finally:
            await self._close_queue()
            await self._close_recorder()

    async def _close_recorder(self) -> None:
        """释放去重记录器的连接（幂等）。

        启动失败与正常停机都要走到：WAL 连接不关会留下 ``-wal``/``-shm``，下次打开
        还得回放一遍。
        """
        recorder = self._idempotency
        if recorder is not None and isinstance(recorder, _ManagedRecorder):
            await recorder.close()

    @abstractmethod
    async def _open_queue(self) -> EventQueue:
        """创建本成员的事件队列。"""

    @abstractmethod
    async def _close_queue(self) -> None:
        """释放本成员的事件队列。"""


class RabbitRuntime(BusRuntime):
    """RabbitMQ fanout 传输：跨进程共享同一逻辑总线。"""

    def __init__(
        self,
        *,
        url: str,
        member_id: str,
        events: Iterable[type[EventDeclaration]],
        strategy: Literal["restart", "offline"] = "restart",
        drain_timeout: float | None = None,
        idempotency: IdempotencyRecorder | None = None,
        intent_path: Path | None = None,
    ) -> None:
        super().__init__(
            member_id=member_id,
            events=events,
            drain_timeout=drain_timeout,
            idempotency=idempotency,
            intent_path=intent_path,
        )
        self._url = url
        self._strategy: Literal["restart", "offline"] = strategy
        self._queue: RabbitFanoutQueue | None = None

    async def _open_queue(self) -> EventQueue:
        queue = await RabbitFanoutQueue.create(
            self._member_id,
            url=self._url,
            registry=self._events,
            strategy=self._strategy,
        )
        self._queue = queue
        return queue

    def set_shutdown_strategy(self, strategy: Literal["restart", "offline"]) -> None:
        """切换停机策略（``blue down`` 前由 supervisor 写下线意图文件触发）。

        ``infinity_bus`` 没有提供运行时切换——``RabbitFanoutQueue`` 在构造时就把
        ``_strategy`` 固定了，而 :meth:`EventQueue.join` 只在停机那一刻读它。所以
        直接改那个字段即可生效。库升级要复核字段名，这里用 ``hasattr`` 探测，
        缺失就保持原策略而不是崩掉。
        """
        queue = self._queue
        if queue is None or not hasattr(queue, "_strategy"):
            logger.warning("队列不支持运行时切换策略，保持 %s", self._strategy)
            return
        setattr(queue, "_strategy", strategy)  # noqa: B010 - 库未暴露公开接口
        self._strategy = strategy

    async def _close_queue(self) -> None:
        queue, self._queue = self._queue, None
        if queue is not None:
            await queue.close()


class MemoryFanout:
    """进程内 fanout 广播中心：一个事件投递给所有在线成员。

    不跨进程、不持久化，仅用于测试与单进程演示。
    """

    def __init__(self) -> None:
        self._members: dict[str, asyncio.Queue[Event]] = {}

    @property
    def members(self) -> tuple[str, ...]:
        """当前在线成员 ID。"""
        return tuple(self._members)

    def attach(self, member_id: str) -> MemoryMemberQueue:
        """为成员创建并登记一条队列。"""
        if member_id in self._members:
            raise ValueError(f"成员 {member_id!r} 已存在")
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._members[member_id] = queue
        return MemoryMemberQueue(self, member_id, queue)

    def detach(self, member_id: str) -> None:
        """注销成员队列（幂等）。"""
        self._members.pop(member_id, None)

    async def broadcast(self, event: Event) -> None:
        """把事件投入所有成员队列（含发布者自己）。"""
        for queue in list(self._members.values()):
            await queue.put(event)


class MemoryMemberQueue(EventQueue):
    """内存 fanout 的成员队列：``put`` 即广播，``get`` 取自己的收件箱。"""

    def __init__(
        self,
        hub: MemoryFanout,
        member_id: str,
        inbox: asyncio.Queue[Event],
    ) -> None:
        self._hub = hub
        self._member_id = member_id
        self._inbox = inbox

    @property
    def member_id(self) -> str:
        """队列所属成员。"""
        return self._member_id

    async def put(self, event: Event) -> None:
        """发布事件到广播中心。"""
        await self._hub.broadcast(event)

    async def get(self) -> Event:
        """取出本成员收件箱中的下一条事件。"""
        return await self._inbox.get()

    def task_done(self) -> None:
        """标记一条事件处理完成。"""
        self._inbox.task_done()

    def qsize(self) -> int:
        """本成员积压事件数。"""
        return self._inbox.qsize()

    async def join(self) -> None:
        """等待本成员积压事件全部处理完成。"""
        await self._inbox.join()


class MemoryRuntime(BusRuntime):
    """进程内总线成员（共享 :class:`MemoryFanout`）。"""

    def __init__(
        self,
        *,
        hub: MemoryFanout,
        member_id: str,
        events: Iterable[type[EventDeclaration]],
    ) -> None:
        super().__init__(member_id=member_id, events=events)
        self._hub = hub
        self._queue: MemoryMemberQueue | None = None

    async def _open_queue(self) -> EventQueue:
        queue = self._hub.attach(self._member_id)
        self._queue = queue
        return queue

    async def _close_queue(self) -> None:
        queue, self._queue = self._queue, None
        if queue is not None:
            self._hub.detach(queue.member_id)
