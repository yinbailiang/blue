"""blue MCP 服务端桥：把 blue 工具目录暴露给外部 MCP 客户端。

```bash
# 列出目录里可暴露的工具（结果写 stderr，stdout 留给协议）
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server blue-service-mcp-server --list-tools

# 作为 stdio MCP server 运行（在 Claude Code / Cursor 的 MCP 配置里指向这条命令）
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server \
  blue-service-mcp-server --expose clock,text_stats,weather
```

``tools/list`` 走 ``catalog.query``，``tools/call`` 走 ``tool.call``；白名单必须
显式给出，``shell`` / ``write_file`` / ``edit_file`` 还要额外 ``--allow-unsafe``。

本服务**不读** ``service_runtime_config.json``：它由人手工运行，配置全部来自命令行
参数，因此 ``service.infd`` 的模板是空的，也不定义 ``CONFIG_MODEL``。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
)
from infinity_bus.templates.request import request
from pydantic import ValidationError

from blue.runtime import build_runtime
from blue_service_mcp_server.config import (
    DEFAULT_PROTOCOL_VERSION,
    DEFAULT_TIMEOUT,
    UNSAFE_TOOLS,
    McpServerSettings,
)
from blue_service_mcp_server.schema import property_to_schema, to_json_schema
from blue_service_mcp_server.server import McpStdioServer, MessageIO, StdioMessageIO

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT",
    "UNSAFE_TOOLS",
    "McpServerSettings",
    "McpStdioServer",
    "MessageIO",
    "StdioMessageIO",
    "build_server",
    "main",
    "property_to_schema",
    "to_json_schema",
]

MEMBER_ID = "ui.mcp_server"
"""总线成员标识前缀（与 supervisor 注入的实例 id 组合）。"""


def build_server(
    settings: McpServerSettings,
    *,
    bus_url: str | None = None,
) -> McpStdioServer:
    """按设置装配一个 stdio MCP server（不启动）。

    :param settings: 白名单 / 超时 / 握手信息。
    :param bus_url: 总线地址；缺省读 ``BLUE_BUS_URL``。
    """
    runtime = build_runtime(MEMBER_ID, url=bus_url)
    return McpStdioServer(runtime=runtime, settings=settings)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.list_tools:
        return asyncio.run(_print_tools(args.bus_url, args.timeout))
    try:
        settings = McpServerSettings(
            expose=_split(args.expose),
            timeout=args.timeout,
            server_name=args.server_name,
            allow_unsafe=args.allow_unsafe,
        )
    except ValidationError as error:
        print(f"错误：{_format_error(error)}", file=sys.stderr)
        return 2
    server = build_server(settings, bus_url=args.bus_url)
    try:
        asyncio.run(server.serve(StdioMessageIO()))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


async def _print_tools(bus_url: str | None, timeout: float) -> int:
    """把目录里可暴露的工具打到 stderr（stdout 留给协议）。"""
    runtime = build_runtime(MEMBER_ID, url=bus_url)
    await runtime.start()
    try:
        response = await request(
            runtime.proxy(MEMBER_ID),
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=timeout,
        )
    except TimeoutError:
        print(f"错误：{timeout:g}s 内没有收到目录响应", file=sys.stderr)
        return 1
    finally:
        await runtime.stop()
    if not isinstance(response, CatalogQueryResponse):
        print("错误：收到非预期的目录响应类型", file=sys.stderr)
        return 1
    print(f"工具目录 v{response.version}（{len(response.tools)} 个）", file=sys.stderr)
    for definition in response.tools:
        function = definition.function
        params = ", ".join(to_json_schema(function.parameters).get("properties", {}))
        danger = " [danger]" if function.name in UNSAFE_TOOLS else ""
        print(f"  - {function.name}({params}){danger}", file=sys.stderr)
        if function.description:
            print(f"      {function.description.splitlines()[0]}", file=sys.stderr)
    return 0


def _split(value: str) -> list[str]:
    """把 ``a,b`` 形式的参数拆成列表（忽略空项与空白）。"""
    return [item.strip() for item in value.split(",") if item.strip()]


def _format_error(error: ValidationError) -> str:
    """把 pydantic 校验错误压成一句人话（命令行不该刷出 schema dump）。"""
    messages = [
        str(item.get("msg", "")).removeprefix("Value error, ")
        for item in error.errors()
    ]
    return "；".join(message for message in messages if message) or str(error)


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="blue-service-mcp-server",
        description="把 blue 工具目录暴露成 stdio MCP server",
    )
    parser.add_argument(
        "--expose",
        default="",
        help="要暴露的工具名，逗号分隔（必填，除非 --list-tools）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次工具调用超时秒数（缺省 {DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--bus-url", default=None, help="总线地址（缺省读 BLUE_BUS_URL）"
    )
    parser.add_argument("--server-name", default="blue", help="握手时报告的 server 名")
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="允许暴露 shell / write_file / edit_file（默认拒绝）",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="只列出目录里的工具（写 stderr）后退出",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
