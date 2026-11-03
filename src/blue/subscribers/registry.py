"""工具注册中心事件处理器：登记 / 续约 / 下线 / 目录查询。"""

from __future__ import annotations

import logging
from typing import Protocol

from infinity_agent import ToolDefinition
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from blue.protocol import (
    CatalogQueryRequest,
    CatalogQueryResponse,
    ToolHeartbeatPayload,
    ToolRegisterPayload,
    ToolUnregisterPayload,
    Topic,
    TopicBook,
)

logger = logging.getLogger(__name__)

__all__ = ["RegistryBackend", "RegistryHandler"]


class RegistryBackend(Protocol):
    """处理器依赖的注册中心能力。"""

    @property
    def topics(self) -> TopicBook:
        """主题表。"""
        ...

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


class RegistryHandler(EventHandler):
    """把注册中心主题事件翻译成后端的状态变更。"""

    def __init__(self, backend: RegistryBackend) -> None:
        topics = backend.topics
        super().__init__(
            subscriptions=[
                topics.name(Topic.TOOL_REGISTER),
                topics.name(Topic.TOOL_HEARTBEAT),
                topics.name(Topic.TOOL_UNREGISTER),
                topics.name(Topic.CATALOG_QUERY),
            ],
            handle_timeout=10.0,
        )
        self._backend = backend

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        """按事件类型分派处理。"""
        del raw_event
        backend = self._backend

        if isinstance(payload, ToolRegisterPayload):
            changed = backend.register_tool(payload)
            logger.info(
                "工具登记：%s（worker=%s, changed=%s）",
                payload.tool.function.name,
                payload.worker_id,
                changed,
            )
            if changed:
                await backend.broadcast_catalog(bus_proxy)
        elif isinstance(payload, ToolHeartbeatPayload):
            if not backend.heartbeat_tool(payload.worker_id):
                logger.warning("收到未登记进程的续约：%s", payload.worker_id)
        elif isinstance(payload, ToolUnregisterPayload):
            if backend.unregister_tool(payload.worker_id):
                logger.info("工具下线：%s", payload.worker_id)
                await backend.broadcast_catalog(bus_proxy)
        elif isinstance(payload, CatalogQueryRequest):
            version, tools = backend.catalog_snapshot()
            await bus_proxy.publish(
                backend.topics.name(Topic.CATALOG_REPLY),
                CatalogQueryResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    version=version,
                    tools=tools,
                ),
            )
