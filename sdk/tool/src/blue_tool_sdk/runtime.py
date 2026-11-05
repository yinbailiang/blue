"""工具进程入口：装配总线运行时并把单个工具跑到退出。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from infinity_bus import EventDeclaration

from blue.runtime import build_runtime, registry_ttl, run_service
from blue_tool_sdk.tool_context import bind_runtime
from blue_tool_sdk.worker import ToolWorker

__all__ = ["run_tool"]


def run_tool(
    tool: Callable[..., Any],
    name: str | None = None,
    *,
    ttl: float | None = None,
    events: Iterable[type[EventDeclaration]] | None = None,
) -> int:
    """把工具函数作为独立进程运行。

    :param tool: 带完整类型签名的工具函数。
    :param name: 工具名（用于成员命名与日志）；缺省取函数名。
    :param ttl: 注册租约；缺省读 supervisor 下发的基础设施配置，再缺省 15 秒。
    :param events: 显式覆盖事件声明。缺省由服务配置里的 ``depends_contracts``
        自动扫描契约包的 ``EVENTS``——工具服务不再手写事件列表。
    """
    tool_name = name or getattr(tool, "__name__", "tool")
    lease = ttl if ttl is not None else registry_ttl()
    runtime = build_runtime(f"tool.{tool_name}", events)
    bind_runtime(runtime)
    worker = ToolWorker(runtime=runtime, tool=tool, ttl=lease)
    return run_service(worker)
