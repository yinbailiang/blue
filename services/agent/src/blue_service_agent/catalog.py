"""Agent 侧的工具目录缓存。

注册中心的租约与目录快照在独立 ``registry`` 服务中；本模块只负责 agent
侧缓存、广播订阅与按需刷新。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolDefinition,
)
from infinity_bus import EventBus
from infinity_bus.templates.request import request

from blue.transport import BusRuntime
from blue_service_agent.subscribers.catalog import CatalogChangedHandler

logger = logging.getLogger(__name__)

__all__ = ["ToolCatalogClient", "schema_chars"]


def schema_chars(tools: Iterable[ToolDefinition]) -> int:
    """工具目录**随请求真正发送**的字符数。

    必须按客户端口径序列化（``exclude_none``）：``ParameterProperty`` 有十几个
    可选字段，全量 dump 会把它们写成 ``"anyOf": null, "enum": null, …``——实测
    比真实请求体大 **2.4 倍**（38 个工具：50,600 vs 20,911 字符）。拿全量数字
    评估每步开销会高估一倍多，进而误判预算。

    :param tools: 工具定义列表。
    :return: 这些定义序列化后的字符总数。
    """
    return sum(len(tool.model_dump_json(exclude_none=True)) for tool in tools)


class ToolCatalogClient:
    """Agent 侧的工具目录缓存。"""

    def __init__(self, *, runtime: BusRuntime) -> None:
        self._runtime = runtime
        self._tools: list[ToolDefinition] = []
        self._version = -1
        self._lock = asyncio.Lock()

    @property
    def tools(self) -> list[ToolDefinition]:
        """缓存中的工具定义。"""
        return list(self._tools)

    @property
    def version(self) -> int:
        """缓存对应的目录版本号（未知为 ``-1``）。"""
        return self._version

    async def start(self) -> None:
        """订阅目录变更广播。"""
        await self._runtime.register(CatalogChangedHandler(self))

    def apply(self, version: int, tools: list[ToolDefinition]) -> bool:
        """应用一份目录快照；版本号不旧于缓存时生效。"""
        if version < self._version:
            return False
        self._version = version
        self._tools = list(tools)
        return True

    async def refresh(
        self,
        proxy: EventBus.Proxy | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[ToolDefinition]:
        """向注册中心拉取最新目录；超时或无注册中心时沿用缓存。"""
        async with self._lock:
            active = proxy or self._runtime.proxy()
            try:
                response = await request(
                    active,
                    CatalogQueryEvent.name,
                    {},
                    CatalogReplyEvent.name,
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning("工具目录查询超时（%.1fs），沿用缓存", timeout)
                return self.tools
            if isinstance(response, CatalogQueryResponse):
                self.apply(response.version, response.tools)
            return self.tools
