"""会话消息处理器：负责 ``session.prompt`` 与 ``session.cancel``。

会话重置与状态查询已迁到独立的 session 服务。

两个事件由总线以独立任务并发派发，因此「中断」不会排在漫长的 prompt
后面——这正是取消能生效的前提。
"""

from __future__ import annotations

from typing import Protocol

from blue_contracts_session import (
    SessionCancelEvent,
    SessionCancelReplyEvent,
    SessionCancelRequest,
    SessionCancelResponse,
    SessionPromptEvent,
    SessionPromptRequest,
    SessionReplyEvent,
    SessionReplyResponse,
)
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

__all__ = ["SessionBackend", "SessionHandler"]


class SessionBackend(Protocol):
    """处理器依赖的会话服务能力。"""

    async def handle_prompt(
        self, payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
    ) -> SessionReplyResponse:
        """处理一条用户消息。"""
        ...

    def cancel(self, session_id: str) -> bool:
        """请求中断该会话正在进行的回复；返回是否确有进行中的 turn。"""
        ...


class SessionHandler(EventHandler):
    """会话消息入口。

    ``handle_timeout=None``：一轮对话可能包含多次模型与工具往返，不能用总线
    默认的 32 秒超时打断；并发由会话锁与总线信号量控制。
    """

    def __init__(self, backend: SessionBackend) -> None:
        super().__init__(
            subscriptions=[SessionPromptEvent.name, SessionCancelEvent.name],
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
        if isinstance(payload, SessionPromptRequest):
            reply = await self._backend.handle_prompt(payload, bus_proxy)
            await bus_proxy.publish(SessionReplyEvent.name, reply)
        elif isinstance(payload, SessionCancelRequest):
            cancelled = self._backend.cancel(payload.session_id)
            await bus_proxy.publish(
                SessionCancelReplyEvent.name,
                SessionCancelResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    cancelled=cancelled,
                ),
            )
