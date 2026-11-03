"""工具目录变更广播的接收处理器（Agent 侧缓存刷新）。"""

from __future__ import annotations

from typing import Protocol

from infinity_agent import ToolDefinition
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from blue.protocol import CatalogChangedPayload, Topic, TopicBook

__all__ = ["CatalogBackend", "CatalogChangedHandler"]


class CatalogBackend(Protocol):
    """处理器依赖的目录缓存能力。"""

    @property
    def topics(self) -> TopicBook:
        """主题表。"""
        ...

    def apply(self, version: int, tools: list[ToolDefinition]) -> bool:
        """应用一份目录快照。"""
        ...


class CatalogChangedHandler(EventHandler):
    """订阅 ``catalog.changed``，把快照写入本地缓存。"""

    def __init__(self, backend: CatalogBackend) -> None:
        super().__init__(
            subscriptions=[backend.topics.name(Topic.CATALOG_CHANGED)],
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
