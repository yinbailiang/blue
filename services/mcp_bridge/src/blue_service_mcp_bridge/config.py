"""MCP 桥的运行时配置与工具命名规则。

配置实例由 supervisor 从 ``blue.infd`` 编译后写入
``services/mcp_bridge/service_runtime_config.json``，服务进程只读。
命名规则集中在 :func:`tool_name` / :func:`worker_id` / :func:`parse_tool_name`，
保证「blue 侧工具名」与「注册中心租约」在桥内只有一处定义。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "TOOL_SEPARATOR",
    "McpBridgeConfig",
    "McpServerConfig",
    "parse_tool_name",
    "tool_name",
    "worker_id",
]

TOOL_SEPARATOR = "__"
"""blue 侧工具名分隔符：``<server>__<tool>``。

工具名进的是 ``ToolDefinition.function.name``（载荷字段），事件名始终是契约里的
静态字符串；前缀存在的理由是注册中心对同名工具采用「新进程接管」，两个 server
提供同名工具时若不区分前缀会静默互相驱逐。
"""

_SERVER_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def tool_name(server: str, remote: str) -> str:
    """拼出 blue 侧工具名：``<server>__<tool>``。"""
    return f"{server}{TOOL_SEPARATOR}{remote}"


def worker_id(server: str, remote: str) -> str:
    """拼出注册中心租约标识：``mcp.<server>.<tool>``。

    不含随机段：进程重启后租约标识不变，注册中心不会在租约过期前留下
    「新进程接管旧记录」的噪声。
    """
    return f"mcp.{server}.{remote}"


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """把 ``<server>__<tool>`` 拆回（server, tool）；格式非法时返回 ``None``。

    server 名不允许包含 ``__``，因此在第一个分隔符处切开无歧义；工具名本身
    可以包含 ``__``。
    """
    head, separator, tail = name.partition(TOOL_SEPARATOR)
    if not separator or not head or not tail:
        return None
    if _SERVER_NAME.match(head) is None:
        return None
    return head, tail


class McpServerConfig(BaseModel):
    """一个 MCP server 的启动参数（stdio 传输）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        description="server 名；blue 工具名的前缀，必须匹配 [a-z][a-z0-9_]*",
    )
    command: str = Field(description="启动命令，例如 npx、uvx、python")
    args: list[str] = Field(default_factory=list[str], description="命令参数")
    env: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="追加或覆盖的环境变量（密钥从 secrets.toml 导入后在此注入）",
    )
    cwd: str | None = Field(
        default=None, description="子进程工作目录；缺省为项目根，相对路径按项目根解析"
    )
    enabled: bool = Field(default=True, description="是否连接并注册该 server 的工具")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """server 名必须是合法的 infd/工具名前缀，且不含分隔符。"""
        if _SERVER_NAME.match(value) is None:
            raise ValueError(f"server 名 {value!r} 非法：必须匹配 [a-z][a-z0-9_]*")
        if TOOL_SEPARATOR in value:
            raise ValueError(f"server 名 {value!r} 不能包含分隔符 {TOOL_SEPARATOR}")
        return value

    @field_validator("command")
    @classmethod
    def _check_command(cls, value: str) -> str:
        """命令不能为空。"""
        if not value.strip():
            raise ValueError("command 不能为空")
        return value


class McpBridgeConfig(BaseModel):
    """MCP 桥的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: list[McpServerConfig] = Field(
        default_factory=list[McpServerConfig], description="要接入的 MCP server"
    )
    tools: list[str] = Field(
        default_factory=list[str],
        description="要暴露的工具白名单，形如 <server>__<tool>；未声明的工具不会注册",
    )
    call_timeout: float = Field(
        default=60.0, gt=0, description="单次 tools/call 的超时（秒）"
    )
    startup_timeout: float = Field(
        default=30.0, gt=0, description="握手与 tools/list 的超时（秒）"
    )
    reconnect_backoff: float = Field(
        default=0.5, gt=0, description="重连退避基数（秒）"
    )
    reconnect_backoff_max: float = Field(
        default=30.0, gt=0, description="重连退避上限（秒）"
    )
    health_interval: float = Field(
        default=5.0, gt=0, description="探活与重连巡检间隔（秒）"
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> McpBridgeConfig:
        """校验 server 名唯一、工具白名单指向已启用的 server 且不重复。

        :raises ValueError: 配置自相矛盾（重复、悬空引用、禁用的 server）。
        """
        names = [server.name for server in self.servers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"server 名重复：{duplicates}")

        known = set(names)
        enabled = {server.name for server in self.servers if server.enabled}
        seen: set[str] = set()
        for entry in self.tools:
            if entry in seen:
                raise ValueError(f"工具 {entry!r} 重复声明")
            seen.add(entry)
            parsed = parse_tool_name(entry)
            if parsed is None:
                raise ValueError(
                    f"工具 {entry!r} 格式非法：应为 <server>{TOOL_SEPARATOR}<tool>"
                )
            server, remote = parsed
            if server not in known:
                raise ValueError(f"工具 {entry!r} 指向未声明的 server {server!r}")
            if server not in enabled:
                raise ValueError(f"工具 {entry!r} 指向已禁用的 server {server!r}")
            if not remote.strip():
                raise ValueError(f"工具 {entry!r} 缺少工具名部分")

        if self.tools and not self.servers:
            raise ValueError("声明了工具但没有声明任何 server")
        if self.reconnect_backoff_max < self.reconnect_backoff:
            raise ValueError("reconnect_backoff_max 不能小于 reconnect_backoff")
        return self

    def server(self, name: str) -> McpServerConfig | None:
        """按名字取 server 配置；未声明时返回 ``None``。"""
        for server in self.servers:
            if server.name == name:
                return server
        return None

    @property
    def enabled_servers(self) -> tuple[McpServerConfig, ...]:
        """全部启用的 server。"""
        return tuple(server for server in self.servers if server.enabled)
