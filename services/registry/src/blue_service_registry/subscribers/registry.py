"""工具注册中心事件处理器：登记 / 续约 / 下线 / 目录查询。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryRequest,
    CatalogReplyEvent,
    ToolDefinition,
    ToolHeartbeatEvent,
    ToolHeartbeatPayload,
    ToolHeartbeatReplyEvent,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolRegisterReplyEvent,
    ToolUnregisterEvent,
    ToolUnregisterPayload,
    ToolUnregisterReplyEvent,
)
from infinity_bus import Event, EventBus
from infinity_bus.templates.handlers.service import (  # pyright: ignore[reportMissingTypeStubs]
    ServiceHandler,
    process,
)

logger = logging.getLogger(__name__)

__all__ = ["RegistryBackend", "RegistryHandler"]


class RegistryBackend(Protocol):
    """处理器依赖的注册中心能力。"""

    def register_tool(self, payload: ToolRegisterPayload) -> bool:
        """登记工具进程，返回目录是否变化。"""
        ...

    def heartbeat_tool(self, worker_id: str) -> bool:
        """刷新租约，返回目标是否仍在册。"""
        ...

    def unregister_tool(self, worker_id: str) -> bool:
        """移除工具进程，返回目录是否变化。"""
        ...

    def catalog_snapshot(self) -> tuple[int, list[ToolDefinition]]:
        """返回 ``(版本号, 工具定义列表)``。"""
        ...

    async def broadcast_catalog(self, proxy: EventBus.Proxy | None = None) -> None:
        """广播当前目录。"""
        ...


class RegistryHandler(ServiceHandler):
    """把注册中心事件翻译成后端的状态变更。

    四个事件现在都是**请求/应答**：注册/续约/下线都会回一句结果，进程能知道
    "登记生效了没、租约还在不在"。注册中心是单实例——以后要支持多实例，应设计
    专用协议再迁移。
    """

    def __init__(self, backend: RegistryBackend) -> None:
        self._backend = backend
        super().__init__()

    @process(ToolRegisterEvent, ToolRegisterReplyEvent)
    async def on_register(
        self, payload: ToolRegisterPayload, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """登记工具进程；目录变了就广播。"""
        del raw_event
        changed = self._backend.register_tool(payload)
        logger.log(
            logging.INFO if changed else logging.DEBUG,
            "工具登记：%s（worker=%s, changed=%s）",
            payload.tool.function.name,
            payload.worker_id,
            changed,
        )
        if changed:
            await self._backend.broadcast_catalog(bus_proxy)
        version, _ = self._backend.catalog_snapshot()
        return {"changed": changed, "version": version}

    @process(ToolHeartbeatEvent, ToolHeartbeatReplyEvent)
    async def on_heartbeat(
        self, payload: ToolHeartbeatPayload, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """续约；未登记的进程要显式告警（多半是注册丢了，调用方会重新登记）。"""
        del bus_proxy, raw_event
        registered = self._backend.heartbeat_tool(payload.worker_id)
        if not registered:
            logger.warning("收到未登记进程的续约：%s", payload.worker_id)
        return {"registered": registered}

    @process(ToolUnregisterEvent, ToolUnregisterReplyEvent)
    async def on_unregister(
        self,
        payload: ToolUnregisterPayload,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """下线；目录变了就广播。"""
        del raw_event
        changed = self._backend.unregister_tool(payload.worker_id)
        if changed:
            logger.info("工具下线：%s", payload.worker_id)
            await self._backend.broadcast_catalog(bus_proxy)
        return {"removed": True, "changed": changed}

    @process(CatalogQueryEvent, CatalogReplyEvent)
    async def on_catalog_query(
        self, payload: CatalogQueryRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """返回当前目录快照（版本号 + 工具定义）。"""
        del bus_proxy, raw_event
        version, tools = self._backend.catalog_snapshot()
        return {"version": version, "tools": tools}
