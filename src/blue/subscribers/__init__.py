"""核心 SDK 内部事件订阅者。

Agent 相关订阅者已迁入 ``services/agent``；核心只保留工具进程使用的
``tool.call`` 处理器。
"""

from __future__ import annotations

from blue.subscribers.tool_call import ToolCallHandler

__all__ = ["ToolCallHandler"]
