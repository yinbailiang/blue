"""MCP 服务端桥的运行设置。

这个方向的服务**不读** ``service_runtime_config.json``：它由人手工运行、直接对接
外部 MCP 客户端（Claude Code / Cursor / Claude Desktop），没有 supervisor 投影，
设置全部来自命令行参数。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT",
    "UNSAFE_TOOLS",
    "McpServerSettings",
]

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
"""客户端未声明协议版本时使用的缺省值。"""

DEFAULT_TIMEOUT = 60.0
"""单次工具调用（``tool.call`` → ``tool.result``）的缺省超时（秒）。"""

UNSAFE_TOOLS = frozenset({"run_shell", "shell", "write_file", "edit_file"})
"""默认不允许暴露的工具。

它们以工具进程权限写文件或执行任意命令，且没有额外沙箱；要暴露必须显式打开
``allow_unsafe``。这类工具交出去等于把宿主机的写权限交给外部客户端。

**注意这里写的是工具名而不是服务名**：``shell`` 服务暴露的工具叫 ``run_shell``，
``clock`` 服务的工具叫 ``now``、``weather`` 的叫 ``get_weather``。名单同时收录
两种写法，避免因为服务/工具命名差异漏掉一个。
"""

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


class McpServerSettings(BaseModel):
    """服务端桥的运行设置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expose: list[str] = Field(
        description="要暴露的 blue 工具名白名单，必须显式列出（不支持「全部」）"
    )
    timeout: float = Field(
        default=DEFAULT_TIMEOUT, gt=0, description="单次工具调用的超时（秒）"
    )
    server_name: str = Field(default="blue", description="握手时报告的 server 名")
    server_version: str = Field(default="0.1.0", description="握手时报告的版本")
    allow_unsafe: bool = Field(
        default=False, description="是否允许暴露 shell / write_file / edit_file"
    )

    @field_validator("expose")
    @classmethod
    def _check_expose(cls, value: list[str]) -> list[str]:
        """工具名必须合法、不重复，且不能为空。"""
        if not value:
            raise ValueError("expose 不能为空：必须显式列出要暴露的工具名")
        duplicates = sorted({name for name in value if value.count(name) > 1})
        if duplicates:
            raise ValueError(f"expose 里工具名重复：{duplicates}")
        for name in value:
            if _TOOL_NAME.match(name) is None:
                raise ValueError(f"工具名 {name!r} 非法：必须匹配 [a-z][a-z0-9_]*")
        return value

    @model_validator(mode="after")
    def _check_unsafe(self) -> McpServerSettings:
        """危险工具必须显式放行。

        :raises ValueError: 白名单含危险工具但未打开 ``allow_unsafe``。
        """
        if self.allow_unsafe:
            return self
        blocked = sorted(set(self.expose) & UNSAFE_TOOLS)
        if blocked:
            raise ValueError(
                f"工具 {blocked} 会以工具进程权限写文件或执行命令；"
                "确需暴露请显式设置 allow_unsafe"
            )
        return self
