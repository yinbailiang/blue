"""测试用 MCP server：只实现桥需要的 stdio JSON-RPC 子集。

以子进程方式被 `test_client.py` 启动，故意包含几个真实 MCP server 常见的行为：

- ``tools/list`` 分页（``nextCursor``）；
- 握手后主动发通知与「服务端请求」（桥应回 ``-32601`` 而不是挂住）；
- ``isError`` 结果、多 content 块（text / image / resource / 未知类型）；
- 慢工具（用于超时路径）；
- ``definitions`` + ``$ref``、无 ``items`` 的 ``array`` 等 schema 形态。

它不依赖 ``mcp`` SDK，只用标准库，因此测试不需要网络。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, cast

PROTOCOL_VERSION = "2025-06-18"
SERVER_REQUEST_ID = 990_001


def _send(message: dict[str, Any]) -> None:
    """向 stdout 写一条 JSON-RPC 消息。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tools_page_one() -> list[dict[str, Any]]:
    """第一页工具。"""
    return [
        {
            "name": "echo",
            "description": "回显文本",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要回显的文本"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "boom",
            "description": "总是失败",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _tools_page_two() -> list[dict[str, Any]]:
    """第二页工具（含各种畸形 schema）。"""
    return [
        {
            "name": "rich",
            "description": "返回多类型 content 块",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "slow",
            "description": "睡眠一段时间",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "zod_schema",
            "description": "zod v3 风格的 definitions + $ref",
            "inputSchema": {
                "type": "object",
                "definitions": {
                    "Filter": {
                        "type": "object",
                        "properties": {"pattern": {"type": "string"}},
                        "required": ["pattern"],
                    }
                },
                "properties": {
                    "filter": {"$ref": "#/definitions/Filter"},
                    "tags": {"type": "array"},
                    "format": {"type": "string", "format": "date-time"},
                    "kind": {"const": "zip"},
                },
                "required": ["filter"],
            },
        },
        {
            "name": "array_no_items",
            "description": "数组参数缺少 items",
            "inputSchema": {
                "type": "object",
                "properties": {"values": {"type": "array"}},
            },
        },
    ]


def _handle_initialize(request_id: Any) -> None:
    """握手响应，并紧接着发通知与服务端请求。"""
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
            },
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "fake server 已就绪"},
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "id": SERVER_REQUEST_ID,
            "method": "sampling/createMessage",
            "params": {},
        }
    )


def _handle_list_tools(request_id: Any, params: dict[str, Any]) -> None:
    """分页返回工具。"""
    if params.get("cursor") == "page2":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": _tools_page_two()},
            }
        )
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": _tools_page_one(), "nextCursor": "page2"},
        }
    )


def _as_arguments(params: dict[str, Any]) -> dict[str, Any]:
    """取 ``arguments`` 字段；非对象一律当作空参数。"""
    raw = params.get("arguments")
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _handle_call_tool(request_id: Any, params: dict[str, Any]) -> None:
    """按工具名分派。"""
    name = params.get("name")
    arguments = _as_arguments(params)
    if name == "echo":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"echo: {arguments.get('text', '')}"}
                    ],
                    "structuredContent": {"echoed": arguments.get("text", "")},
                },
            }
        )
        return
    if name == "boom":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "工具内部错误"}],
                    "isError": True,
                },
            }
        )
        return
    if name == "rich":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "image", "mimeType": "image/png", "data": "QUJDRA=="},
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "file:///tmp/report.txt",
                                "mimeType": "text/plain",
                                "text": "资源正文",
                            },
                        },
                        {"type": "audio", "mimeType": "audio/wav", "data": "AAAA"},
                        {"type": "future_block", "payload": {"x": 1}},
                    ]
                },
            }
        )
        return
    if name == "slow":
        time.sleep(3.0)
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "终于好了"}]},
            }
        )
        return
    if name in {"zod_schema", "array_no_items"}:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": f"{name} 已执行"}]},
            }
        )
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"未知工具 {name!r}"},
        }
    )


def _handle(message: dict[str, Any]) -> None:
    """处理一条入站消息。"""
    if "method" not in message:
        # 服务端请求的响应：本 fake 不关心，同步测试只验证桥收到了响应。
        if message.get("id") == SERVER_REQUEST_ID:
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "已收到对服务端请求的响应"},
                }
            )
        return
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    payload = cast(dict[str, Any], params) if isinstance(params, dict) else {}
    if request_id is None:
        return
    if method == "initialize":
        _handle_initialize(request_id)
        return
    if method == "tools/list":
        _handle_list_tools(request_id, payload)
        return
    if method == "tools/call":
        _handle_call_tool(request_id, payload)
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"未实现的方法 {method!r}"},
        }
    )


def main() -> int:
    """从 stdin 逐行读消息，直到 EOF。"""
    print("fake-mcp-server ready", file=sys.stderr, flush=True)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            _handle(cast(dict[str, Any], message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
