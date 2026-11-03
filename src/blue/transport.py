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
from abc import ABC, abstractmethod
from typing import Literal

from infinity_bus import (
    Event,
    EventBus,
    EventHandler,
    EventHandlerRegistry,
    EventQueue,
    EventRegistry,
)
from infinity_bus.templates.queues import RabbitFanoutQueue

from blue.protocol import TopicBook

__all__ = [
    "BusRuntime",
    "MemoryFanout",
    "MemoryMemberQueue",
    "MemoryRuntime",
    "RabbitRuntime",
]


class BusRuntime(ABC):
    """总线成员运行时：统一事件注册、处理器注册与生命周期。"""

    def __init__(self, *, member_id: str, topics: TopicBook) -> None:
        if not member_id.strip():
            raise ValueError("member_id 不能为空")
        self._member_id = member_id
        self._topics = topics
        self._events = EventRegistry()
        topics.register(self._events)
        self._handlers = EventHandlerRegistry()
        self._bus: EventBus | None = None

    @property
    def member_id(self) -> str:
        """本成员在总线上的唯一标识。"""
        return self._member_id

    @property
    def topics(self) -> TopicBook:
        """主题表。"""
        return self._topics

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
        queue = await self._open_queue()
        bus = EventBus(self._events, self._handlers, queue=queue)
        self._bus = bus
        try:
            await bus.start()
        except Exception:
            self._bus = None
            await self._close_queue()
            raise

    async def stop(self) -> None:
        """停止总线并释放队列（幂等）。"""
        bus, self._bus = self._bus, None
        try:
            if bus is not None:
                await bus.stop()
        finally:
            await self._close_queue()

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
        topics: TopicBook,
        strategy: Literal["restart", "offline"] = "restart",
    ) -> None:
        super().__init__(member_id=member_id, topics=topics)
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
        topics: TopicBook,
    ) -> None:
        super().__init__(member_id=member_id, topics=topics)
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
