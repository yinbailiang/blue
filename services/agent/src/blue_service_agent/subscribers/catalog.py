"""工具目录变更事件的接收处理器（Agent 侧缓存刷新）。"""

from __future__ import annotations

from typing import Protocol

from blue_contracts_tool import CatalogChangedEvent, CatalogChangedPayload
from infinity_agent import ToolDefinition
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

__all__ = ["CatalogBackend", "CatalogChangedHandler"]


class CatalogBackend(Protocol):
    """处理器依赖的目录缓存能力。"""

    def apply(self, version: int, tools: list[ToolDefinition]) -> bool:
        """应用一份目录快照。"""
        ...


class CatalogChangedHandler(EventHandler):
    """订阅目录变更事件，把快照写入本地缓存。"""

    def __init__(self, backend: CatalogBackend) -> None:
        super().__init__(
            subscriptions=[CatalogChangedEvent.name],
            handle_timeout=5.0,
        )
        self._backend = backend

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, CatalogChangedPayload):
            self._backend.apply(payload.version, payload.tools)
