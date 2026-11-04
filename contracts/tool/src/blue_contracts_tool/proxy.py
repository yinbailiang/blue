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
    ToolHeartbeatReplyEvent,
    ToolHeartbeatResponse,
    ToolRegisterEvent,
    ToolRegisterReplyEvent,
    ToolRegisterResponse,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterReplyEvent,
    ToolUnregisterResponse,
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
        timeout: float | None = None,
    ) -> ToolRegisterResponse:
        """登记工具进程，返回目录是否变化。

        注册中心是**单实例**：请求/应答能明确告知"登记成功/租约冲突"，比单向
        广播可靠。以后要支持多实例再设计专用协议迁移。
        """
        return await self._call(
            ToolRegisterEvent,
            ToolRegisterReplyEvent,
            ToolRegisterResponse,
            {"worker_id": worker_id, "tool": tool, "ttl": ttl},
            session_id=None,
            timeout=timeout,
        )

    async def heartbeat(
        self, worker_id: str, *, timeout: float | None = None
    ) -> ToolHeartbeatResponse:
        """续约；``registered=False`` 表示租约已失效、需要重新登记。"""
        return await self._call(
            ToolHeartbeatEvent,
            ToolHeartbeatReplyEvent,
            ToolHeartbeatResponse,
            {"worker_id": worker_id},
            session_id=None,
            timeout=timeout,
        )

    async def unregister(
        self, worker_id: str, *, timeout: float | None = None
    ) -> ToolUnregisterResponse:
        """主动下线，返回是否真的移除了登记。"""
        return await self._call(
            ToolUnregisterEvent,
            ToolUnregisterReplyEvent,
            ToolUnregisterResponse,
            {"worker_id": worker_id},
            session_id=None,
            timeout=timeout,
        )

    async def publish_catalog(self, version: int, tools: list[ToolDefinition]) -> None:
        """广播目录变更（注册中心用）。"""
        await self._bus.publish(
            CatalogChangedEvent.name,
            CatalogChangedPayload(version=version, tools=tools),
        )
