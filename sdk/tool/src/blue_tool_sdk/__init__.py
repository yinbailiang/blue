"""工具域运行时：把工具函数接入总线、登记租约并携带调用上下文。

这个包是 ``blue`` 核心之外的**独立运行时包**：只有工具类服务依赖它，
``blue`` 本身不依赖任何契约包。事件注册由服务配置（``depends_contracts``）
驱动，见 :func:`run_tool` 与 ``blue.runtime.build_runtime``。
"""

from blue_tool_sdk.runtime import run_tool
from blue_tool_sdk.tool_call import ToolCallBackend, ToolCallHandler
from blue_tool_sdk.tool_context import (
    ToolContext,
    bind_runtime,
    bind_runtime_scoped,
    call_service,
    current_context,
    proxy,
    session_proxy,
    use_context,
)
from blue_tool_sdk.worker import (
    ToolLease,
    ToolRegistrar,
    ToolWorker,
    unpack_tool_output,
)

__all__ = [
    "ToolCallBackend",
    "ToolCallHandler",
    "ToolContext",
    "ToolLease",
    "ToolRegistrar",
    "ToolWorker",
    "bind_runtime",
    "bind_runtime_scoped",
    "call_service",
    "current_context",
    "proxy",
    "run_tool",
    "session_proxy",
    "unpack_tool_output",
    "use_context",
]
