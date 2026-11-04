"""MCP 桥端到端测试：真实子进程 MCP server → 桥 → 总线 → 结果（含图片）。

用的是仓库里的样例 server（``mcp_servers/virtual_screen/server.py``），
因此这条用例同时验证「外部 MCP server 能被拉起并注册」与「调用与图片附件
能原样回到调用方」——这正是 MCP 桥可行性要回答的两件事。
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

from blue_contracts_tool import ToolCallEvent, ToolCallResult, ToolResultEvent
from blue_service_mcp_bridge import McpBridge, McpBridgeConfig, McpServerConfig
from infinity_bus.templates.request import request
from tests.support import make_runtime, wait_until

from blue.transport import MemoryFanout

SERVER = (
    Path(__file__).resolve().parents[3] / "mcp_servers" / "virtual_screen" / "server.py"
)
"""仓库内样例 MCP server。"""

TOOLS = [
    "vscreen__screen_info",
    "vscreen__screen_clear",
    "vscreen__screen_fill",
    "vscreen__screen_rect",
    "vscreen__screen_text",
    "vscreen__screen_snapshot",
]


def test_bridge_registers_and_calls_external_mcp_server() -> None:
    """外部 MCP server 的工具能被注册、调用，并把图片带回来。"""
    asyncio.run(_bridge_roundtrip())


async def _bridge_roundtrip() -> None:
    hub = MemoryFanout()
    bridge = McpBridge(
        runtime=make_runtime(hub, "mcp_bridge"),
        config=McpBridgeConfig(
            servers=[
                McpServerConfig(
                    name="vscreen",
                    command=sys.executable,
                    args=[str(SERVER)],
                )
            ],
            tools=TOOLS,
            call_timeout=20.0,
        ),
        ttl=5.0,
    )
    chat = make_runtime(hub, "chat")
    await bridge.start()
    await chat.start()
    try:
        await wait_until(lambda: set(bridge.tools) == set(TOOLS))

        filled = await _call(chat, "vscreen__screen_fill", {"color": "#0f172a"})
        assert filled.success is True
        assert "#0f172a" in filled.content

        written = await _call(
            chat,
            "vscreen__screen_text",
            {"text": "hello MCP", "x": 40, "y": 110, "color": "#5ee2a8"},
        )
        assert written.success is True
        assert "hello MCP" in written.content

        snapshot = await _call(chat, "vscreen__screen_snapshot", {"note": "测试"})
        assert snapshot.success is True
        assert "虚拟屏幕快照" in snapshot.content
        assert len(snapshot.attachments) == 1
        attachment = snapshot.attachments[0]
        assert attachment.mime == "image/png"
        data = base64.b64decode(attachment.data)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

        broken = await _call(chat, "vscreen__screen_fill", {"color": "not-a-color"})
        assert broken.success is False
        assert "颜色" in (broken.error_msg or "")
    finally:
        await chat.stop()
        await bridge.stop()


async def _call(
    chat: object, tool: str, arguments: dict[str, object]
) -> ToolCallResult:
    """通过总线调用一个由 MCP server 提供的工具。"""
    from blue.transport import MemoryRuntime

    assert isinstance(chat, MemoryRuntime)
    result = await request(
        chat.proxy(),
        ToolCallEvent.name,
        {"tool": tool, "arguments": arguments},
        ToolResultEvent.name,
        session_id="mcp-test",
        timeout=20.0,
    )
    assert isinstance(result, ToolCallResult)
    return result
