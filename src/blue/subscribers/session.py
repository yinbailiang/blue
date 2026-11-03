"""会话事件处理器：消息 / 重置 / 状态查询。"""

from __future__ import annotations

from typing import Protocol

from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from blue.protocol import (
    SessionPromptEvent,
    SessionPromptRequest,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetRequest,
    SessionResetResponse,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateRequest,
    SessionStateResponse,
)

__all__ = ["SessionBackend", "SessionHandler"]


class SessionBackend(Protocol):
    """处理器依赖的会话服务能力。"""

    async def handle_prompt(
        self, payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
    ) -> SessionReplyResponse:
        """处理一条用户消息。"""
        ...

    def handle_reset(self, payload: SessionResetRequest) -> SessionResetResponse:
        """重置会话。"""
        ...

    def handle_state(self, payload: SessionStateRequest) -> SessionStateResponse:
        """导出会话元信息。"""
        ...


class SessionHandler(EventHandler):
    """会话事件入口。

    ``handle_timeout=None``：一轮对话可能包含多次模型与工具往返，不能用总线
    默认的 32 秒超时打断；并发由会话锁与总线信号量控制。
    """

    def __init__(self, backend: SessionBackend) -> None:
        super().__init__(
            subscriptions=[
                SessionPromptEvent.name,
                SessionResetEvent.name,
                SessionStateEvent.name,
            ],
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
        backend = self._backend

        if isinstance(payload, SessionPromptRequest):
            reply = await backend.handle_prompt(payload, bus_proxy)
            await bus_proxy.publish(SessionReplyEvent.name, reply)
        elif isinstance(payload, SessionResetRequest):
            await bus_proxy.publish(
                SessionResetReplyEvent.name, backend.handle_reset(payload)
            )
        elif isinstance(payload, SessionStateRequest):
            await bus_proxy.publish(
                SessionStateReplyEvent.name, backend.handle_state(payload)
            )
