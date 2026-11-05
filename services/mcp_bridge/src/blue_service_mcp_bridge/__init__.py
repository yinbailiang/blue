"""blue 外置工具：MCP 客户端桥。

把外部 MCP server（stdio 传输）的工具接入 blue 工具目录，让 agent 用与本地工具
完全一致的方式调用它们：

```infd
!from p"services/mcp_bridge/service.infd" import ServiceTemplate as McpBridgeService

mcp_bridge = McpBridgeService(
  tools = ["fs__read_file", "git__status"],
  servers = [
    {name = "fs", command = "npx", args = ["-y", "server-filesystem", "/tmp"]},
    {name = "git", command = "uvx", args = ["mcp-server-git"]},
  ],
)
```

工具集是声明式的：``tools`` 白名单里的 ``<server>__<tool>`` 才会注册，运行期
不跟随 MCP 的 ``tools/list_changed``，只有 ``blue apply`` 才会对齐。声明的工具
在 server 上不存在时桥启动失败（fail loud），不会静默少一个工具。
"""

from __future__ import annotations

from blue.runtime import build_runtime, load_runtime_config, registry_ttl, run_service
from blue_service_mcp_bridge.client import (
    McpCallError,
    McpCallResult,
    McpConnector,
    McpError,
    McpProtocolError,
    McpSession,
    McpTimeoutError,
    McpToolInfo,
    McpTransportError,
    StdioConnector,
    StdioMcpSession,
)
from blue_service_mcp_bridge.config import (
    McpBridgeConfig,
    McpServerConfig,
    parse_tool_name,
    tool_name,
    worker_id,
)
from blue_service_mcp_bridge.schema import SchemaConversion, convert_input_schema
from blue_service_mcp_bridge.worker import BoundTool, McpBridge

__all__ = [
    "CONFIG_MODEL",
    "SERVICE_NAME",
    "BoundTool",
    "McpBridge",
    "McpBridgeConfig",
    "McpCallError",
    "McpCallResult",
    "McpConnector",
    "McpError",
    "McpProtocolError",
    "McpServerConfig",
    "McpSession",
    "McpTimeoutError",
    "McpToolInfo",
    "McpTransportError",
    "SchemaConversion",
    "StdioConnector",
    "StdioMcpSession",
    "build_bridge",
    "convert_input_schema",
    "main",
    "parse_tool_name",
    "tool_name",
    "worker_id",
]

CONFIG_MODEL = McpBridgeConfig
"""契约测试使用的运行时配置模型。"""

SERVICE_NAME = "mcp_bridge"
"""服务名，同时作为总线成员前缀。"""


def build_bridge(
    config: McpBridgeConfig,
    *,
    connector: McpConnector | None = None,
    ttl: float | None = None,
) -> McpBridge:
    """按配置装配一个桥（不启动）。

    :param config: 桥配置。
    :param connector: 会话工厂；缺省按配置构造 stdio 连接器。
    :param ttl: 工具租约时长；缺省读 ``BLUE_REGISTRY_TTL``。
    """
    return McpBridge(
        runtime=build_runtime(SERVICE_NAME),
        config=config,
        ttl=registry_ttl() if ttl is None else ttl,
        connector=connector,
    )


def main() -> int:
    """控制台脚本入口。"""
    return run_service(build_bridge(load_runtime_config(CONFIG_MODEL)))


if __name__ == "__main__":
    raise SystemExit(main())
