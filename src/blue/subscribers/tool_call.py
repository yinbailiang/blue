"""工具调用处理器：订阅 ``tool.call`` 并交由工具进程执行。"""

from __future__ import annotations

from typing import Protocol

from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from blue.protocol import ToolCallEvent, ToolCallRequest

__all__ = ["ToolCallBackend", "ToolCallHandler"]


class ToolCallBackend(Protocol):
    """处理器依赖的工具进程能力。"""

    async def handle_call(
        self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """执行（或忽略）一次工具调用。"""
        ...


class ToolCallHandler(EventHandler):
    """订阅全部工具调用，按 ``payload.tool`` 在进程内过滤。

    调用超时由发起方控制，处理器不设超时，避免长耗时工具被中途取消。
    """

    def __init__(self, backend: ToolCallBackend) -> None:
        super().__init__(
            subscriptions=[ToolCallEvent.name],
            handle_timeout=None,
        )
        self._backend = backend

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del raw_event
        if isinstance(payload, ToolCallRequest):
            await self._backend.handle_call(payload, bus_proxy)
