"""本体聚合内部的事件订阅者。

每个服务对象（:class:`~blue.agent.AgentCore` /
:class:`~blue.catalog.ToolRegistryService` / :class:`~blue.catalog.ToolCatalogClient`
/ :class:`~blue.worker.ToolWorker`）把自己的事件入口托管在这里：一个模块一个
订阅者，只做「事件 → 服务能力」的翻译。

订阅者通过各模块内的 ``*Backend`` 窄协议声明依赖，不反向导入服务模块，既避免
循环依赖，也便于用假实现单测。

> 外置程序（工具进程、用户界面）不在这里，它们在仓库根目录的 ``handlers/``。
"""

from __future__ import annotations

from blue.subscribers.catalog import CatalogChangedHandler
from blue.subscribers.registry import RegistryHandler
from blue.subscribers.session import SessionHandler
from blue.subscribers.tool_call import ToolCallHandler

__all__ = [
    "CatalogChangedHandler",
    "RegistryHandler",
    "SessionHandler",
    "ToolCallHandler",
]
