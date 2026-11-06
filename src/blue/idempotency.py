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

存储复用库里的 :class:`~infinity_bus.templates.SqliteIdempotencyRecorder`
（``INSERT OR IGNORE`` + 主键兜底，重复标记本身幂等安全）。
"""

from __future__ import annotations

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

__all__ = ["IdempotencyMiddleware"]


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
