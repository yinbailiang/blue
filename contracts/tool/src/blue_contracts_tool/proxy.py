"""工具与注册中心的强类型调用代理。

和 ``SessionProxy`` 同样的理由：请求/应答配对写在契约里，代理就放在契约里。
一个 ``ToolProxy`` 同时覆盖两件事：

- **调用工具**：``call`` → ``tool.call`` / ``tool.result``；
- **注册中心管理**：``register`` / ``heartbeat`` / ``unregister`` / ``publish_catalog``
  是**单向广播**（没有应答），这里只负责把载荷拼对、发出去；
  另有 ``catalog`` → ``catalog.query`` / ``catalog.reply``。

单向事件用 ``publish_*`` 语义的方法名，一眼能看出"发了就走，不等回话"。
"""

from __future__ import annotations

from typing import Any

from infinity_bus import EventBus, EventDeclaration
from infinity_bus.templates.request import ResponseProtocol, request

from blue_contracts_tool.events import (
    CatalogChangedEvent,
    CatalogChangedPayload,
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolCallEvent,
    ToolCallResult,
    ToolDefinition,
    ToolHeartbeatEvent,
    ToolHeartbeatPayload,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterPayload,
)

__all__ = ["ToolCallError", "ToolProxy"]


class ToolCallError(Exception):
    """工具 RPC 失败：超时、应答类型不符，或工具进程返回 ``success=False``。"""


class ToolProxy:
    """工具与注册中心的客户端代理。

    :param bus_proxy: 当前进程的总线出口。
    :param timeout: 单次调用超时（秒），可在方法上覆盖。
    """

    def __init__(self, bus_proxy: EventBus.Proxy, *, timeout: float = 30.0) -> None:
        self._bus = bus_proxy
        self._timeout = timeout

    # -- 内部 ----------------------------------------------------------

    async def _call(
        self,
        request_event: type[EventDeclaration],
        reply_event: type[EventDeclaration],
        reply_type: type[ResponseProtocol],
        payload: dict[str, Any],
        *,
        session_id: str | None,
        timeout: float | None = None,
    ) -> Any:
        """发请求 → 收窄类型 → 失败转异常。"""
        try:
            response = await request(
                self._bus,
                request_event.name,
                payload,
                reply_event.name,
                session_id=session_id,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except TimeoutError as error:
            raise ToolCallError(f"{request_event.name} 等待响应超时") from error
        if not isinstance(response, reply_type):
            raise ToolCallError(
                f"{request_event.name} 收到非预期应答：{type(response).__name__}"
            )
        if not response.success:
            raise ToolCallError(response.error_msg or f"{request_event.name} 失败")
        return response

    # -- 调用工具 ------------------------------------------------------

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> ToolCallResult:
        """调用一个工具，返回结果（含正文与结构化附件）。"""
        return await self._call(
            ToolCallEvent,
            ToolResultEvent,
            ToolCallResult,
            {"tool": tool, "arguments": arguments or {}},
            session_id=session_id,
            timeout=timeout,
        )

    # -- 注册中心 ------------------------------------------------------

    async def catalog(self, *, timeout: float | None = None) -> CatalogQueryResponse:
        """查询当前工具目录。"""
        return await self._call(
            CatalogQueryEvent,
            CatalogReplyEvent,
            CatalogQueryResponse,
            {},
            session_id=None,
            timeout=timeout,
        )

    async def register(
        self,
        worker_id: str,
        tool: ToolDefinition,
        *,
        ttl: float,
    ) -> None:
        """登记工具进程（单向广播，不等回话）。"""
        await self._bus.publish(
            ToolRegisterEvent.name,
            ToolRegisterPayload(worker_id=worker_id, tool=tool, ttl=ttl),
        )

    async def heartbeat(self, worker_id: str) -> None:
        """续约（单向广播）。"""
        await self._bus.publish(
            ToolHeartbeatEvent.name, ToolHeartbeatPayload(worker_id=worker_id)
        )

    async def unregister(self, worker_id: str) -> None:
        """主动下线（单向广播）。"""
        await self._bus.publish(
            ToolUnregisterEvent.name, ToolUnregisterPayload(worker_id=worker_id)
        )

    async def publish_catalog(self, version: int, tools: list[ToolDefinition]) -> None:
        """广播目录变更（注册中心用）。"""
        await self._bus.publish(
            CatalogChangedEvent.name,
            CatalogChangedPayload(version=version, tools=tools),
        )
