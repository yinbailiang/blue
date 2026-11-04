"""stdio MCP 客户端测试：用真实的假 server 子进程跑完整协议。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from blue_service_mcp_bridge.client import (
    McpCallError,
    McpTimeoutError,
    McpTransportError,
    StdioConnector,
    StdioMcpSession,
)
from blue_service_mcp_bridge.config import McpServerConfig

FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"
"""测试用 MCP server 脚本（标准库实现，无需网络）。"""


def _server() -> McpServerConfig:
    """指向 fake server 的启动配置。"""
    return McpServerConfig(
        name="fake",
        command=sys.executable,
        args=[str(FAKE_SERVER)],
    )


async def _open(timeout: float = 5.0) -> StdioMcpSession:
    """启动并握手一个 fake server 会话。"""
    connector = StdioConnector(cwd=FAKE_SERVER.parent, timeout=timeout)
    session = await connector.connect(_server())
    assert isinstance(session, StdioMcpSession)
    return session


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """轮询等待条件成立（stderr 采集是异步的）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


def test_lists_tools_across_pages() -> None:
    """``tools/list`` 会自动翻页并聚合结果。"""
    asyncio.run(_lists_tools())


async def _lists_tools() -> None:
    session = await _open()
    try:
        tools = await session.list_tools()
        assert [tool.name for tool in tools] == [
            "echo",
            "boom",
            "rich",
            "huge",
            "slow",
            "zod_schema",
            "array_no_items",
        ]
        assert tools[0].description == "回显文本"
        assert tools[0].input_schema["type"] == "object"
    finally:
        await session.close()


def test_calls_tool_and_appends_structured_content() -> None:
    """文本结果与 ``structuredContent`` 都会进入返回内容。"""
    asyncio.run(_call_echo())


async def _call_echo() -> None:
    session = await _open()
    try:
        result = await session.call_tool("echo", {"text": "ping"})
        assert result.is_error is False
        assert "echo: ping" in result.content
        assert '"echoed": "ping"' in result.content
    finally:
        await session.close()


def test_maps_is_error_result() -> None:
    """``isError: true`` 原样映射到结果，不抛异常。"""
    asyncio.run(_call_boom())


async def _call_boom() -> None:
    session = await _open()
    try:
        result = await session.call_tool("boom", {})
        assert result.is_error is True
        assert result.content == "工具内部错误"
    finally:
        await session.close()


def test_flattens_rich_content_blocks() -> None:
    """图片 / 资源 / 音频 / 未知块都降级成可读占位。"""
    asyncio.run(_call_rich())


async def _call_rich() -> None:
    session = await _open()
    try:
        result = await session.call_tool("rich", {})
        assert result.is_error is False
        assert "第一段" in result.content
        # 图片不再是占位文本，而是结构化附件（由 agent 转成内容寻址存储）
        assert [item.mime for item in result.attachments] == ["image/png"]
        assert result.attachments[0].data == "QUJDRA=="
        assert "[resource: file:///tmp/report.txt]" in result.content
        assert "资源正文" in result.content
        assert "[audio: audio/wav]" in result.content
        assert "future_block" in result.content
    finally:
        await session.close()


def test_unknown_tool_raises_call_error() -> None:
    """JSON-RPC error 转成 ``McpCallError`` 并带错误码。"""
    asyncio.run(_call_unknown())


async def _call_unknown() -> None:
    session = await _open()
    try:
        with pytest.raises(McpCallError) as captured:
            await session.call_tool("nope", {})
        assert "未知工具" in str(captured.value)
        assert captured.value.code == -32602
    finally:
        await session.close()


def test_call_timeout_raises_timeout_error() -> None:
    """超时走 ``McpTimeoutError``，与传输错误区分开。"""
    asyncio.run(_call_slow())


async def _call_slow() -> None:
    session = await _open()
    try:
        with pytest.raises(McpTimeoutError):
            await session.call_tool("slow", {}, timeout=0.2)
    finally:
        await session.close()


def test_close_is_idempotent_and_fails_later_calls() -> None:
    """关闭后会话不可用，再次调用立即失败而不是挂住。"""
    asyncio.run(_close_twice())


async def _close_twice() -> None:
    session = await _open()
    await session.close()
    await session.close()
    assert session.is_alive is False
    with pytest.raises(McpTransportError):
        await session.call_tool("echo", {"text": "x"})


def test_captures_stderr_for_diagnostics() -> None:
    """stderr 被收进环形缓冲，失败时能用上。"""
    asyncio.run(_capture_stderr())


async def _capture_stderr() -> None:
    session = await _open()
    try:
        await _wait_for(lambda: any("ready" in line for line in session.stderr_tail))
        assert any("fake-mcp-server ready" in line for line in session.stderr_tail)
    finally:
        await session.close()


def test_reads_oversized_message() -> None:
    """单个响应超过 64 KiB 管道默认上限时也要能读到（截图就是这种体量）。"""
    asyncio.run(_reads_oversized())


async def _reads_oversized() -> None:
    session = await _open(timeout=20.0)
    try:
        result = await session.call_tool("huge", {}, timeout=20.0)
        assert result.is_error is False
        assert len(result.content) >= 1024 * 1024
    finally:
        await session.close()
