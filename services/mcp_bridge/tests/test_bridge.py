"""MCP 桥测试：注册、调用、失败语义、重连与停机。

用进程内总线（``MemoryFanout``）验证桥与控制面的契约，用假会话验证桥自身的
失败处理，不依赖真实 MCP server 或网络。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import cast

import pytest
from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from blue_service_mcp_bridge import (
    McpBridge,
    McpBridgeConfig,
    McpCallResult,
    McpConnector,
    McpError,
    McpServerConfig,
    McpSession,
    McpTimeoutError,
    McpToolInfo,
    McpTransportError,
)
from blue_service_mcp_bridge.client import McpAttachment
from blue_service_registry import ToolRegistryService
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import make_runtime, wait_until

from blue.transport import MemoryFanout

TOOL_NAME = "fs__read_file"
"""测试统一使用的 blue 工具名。"""

WORKER_ID = "mcp.fs.read_file"
"""测试统一使用的租约标识（确定性，不含随机段）。"""


def _read_file_tool() -> McpToolInfo:
    """一个普通工具。"""
    return McpToolInfo(
        name="read_file",
        description="读取文件",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
    )


def _broken_schema_tool() -> McpToolInfo:
    """参数 schema 需要降级的工具（``array`` 缺 ``items``）。"""
    return McpToolInfo(
        name="read_file",
        description="读取文件",
        input_schema={
            "type": "object",
            "properties": {"paths": {"type": "array"}},
        },
    )


def _tools(**overrides: McpToolInfo) -> list[McpToolInfo]:
    """server 的工具清单，可被用例替换。"""
    if overrides:
        return list(overrides.values())
    return [
        _read_file_tool(),
        McpToolInfo(name="extra", description="未声明的工具", input_schema={}),
    ]


class FakeSession:
    """内存版 MCP 会话：可编程返回结果或抛异常。"""

    def __init__(
        self,
        server_name: str,
        tools: list[McpToolInfo],
        *,
        list_error: Exception | None = None,
        call: Callable[[str, dict[str, object]], McpCallResult] | None = None,
    ) -> None:
        self._server_name = server_name
        self._tools = tools
        self._list_error = list_error
        self._call = call
        self.alive = True
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def server_name(self) -> str:
        """该会话对应的 server 名。"""
        return self._server_name

    @property
    def is_alive(self) -> bool:
        """会话是否可用（用例可手动置 False 模拟崩溃）。"""
        return self.alive and not self.closed

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """假会话没有 stderr。"""
        return ()

    async def list_tools(self) -> list[McpToolInfo]:
        """返回预置工具清单，或抛出预置错误。"""
        if self._list_error is not None:
            raise self._list_error
        return list(self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> McpCallResult:
        """记录调用并按预置行为返回。"""
        del timeout
        self.calls.append((name, dict(arguments)))
        if self._call is not None:
            return self._call(name, arguments)
        payload = json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)
        return McpCallResult(content=payload, is_error=False)

    async def close(self) -> None:
        """关闭会话。"""
        self.closed = True


class FakeConnector:
    """按配置生产 :class:`FakeSession` 的连接器。"""

    def __init__(
        self,
        tools: dict[str, list[McpToolInfo]],
        *,
        list_error: Exception | None = None,
        call: Callable[[str, dict[str, object]], McpCallResult] | None = None,
        failures: int = 0,
    ) -> None:
        self._tools = tools
        self._list_error = list_error
        self._call = call
        self.failures = failures
        self.connects: list[str] = []
        self.created: list[FakeSession] = []

    async def connect(self, server: McpServerConfig) -> McpSession:
        """记录连接；``failures`` 大于 0 时先失败若干次。"""
        self.connects.append(server.name)
        if self.failures > 0:
            self.failures -= 1
            raise McpTransportError(f"无法连接 {server.name}")
        session = FakeSession(
            server.name,
            self._tools.get(server.name, []),
            list_error=self._list_error,
            call=self._call,
        )
        self.created.append(session)
        return session


class RecordingProxy:
    """记录 ``publish`` 调用的总线代理替身。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish(self, name: str, payload: object) -> None:
        """记下事件名与载荷。"""
        self.published.append((name, payload))


def _config(
    *,
    servers: tuple[str, ...] = ("fs",),
    tools: list[str] | None = None,
    call_timeout: float = 5.0,
) -> McpBridgeConfig:
    """构造一份桥配置。"""
    return McpBridgeConfig.model_validate(
        {
            "servers": [
                {"name": name, "command": "python", "args": ["fake"]}
                for name in servers
            ],
            "tools": tools if tools is not None else [TOOL_NAME],
            "call_timeout": call_timeout,
            "health_interval": 0.05,
        }
    )


def _bridge(
    hub: MemoryFanout, config: McpBridgeConfig, connector: McpConnector
) -> McpBridge:
    """在给定 hub 上装配桥。"""
    return McpBridge(
        runtime=make_runtime(hub, "mcp_bridge"),
        config=config,
        ttl=5.0,
        connector=connector,
    )


def test_bridge_registers_declared_tools_only() -> None:
    """只注册白名单里的工具，租约标识确定性可预测。"""
    asyncio.run(_registers_declared_tools())


async def _registers_declared_tools() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    connector = FakeConnector({"fs": _tools()})
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await registry.start()
    await bridge.start()
    await client.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)
        assert registry.state.record(WORKER_ID) is not None
        catalog = await request(
            client.proxy(),
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        assert [tool.function.name for tool in catalog.tools] == [TOOL_NAME]
        description = catalog.tools[0].function.description
        assert description == "读取文件"
        parameters = catalog.tools[0].function.parameters
        assert set(parameters.properties) == {"path"}
        assert parameters.required == ["path"]
    finally:
        await client.stop()
        await bridge.stop()
        await registry.stop()


def test_bridge_serves_tool_call() -> None:
    """通过总线调用时，桥把参数原样转给 MCP 工具并回传结果。"""
    asyncio.run(_serves_call())


async def _serves_call() -> None:
    hub = MemoryFanout()
    connector = FakeConnector({"fs": _tools()})
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "a.txt"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert result.error_msg is None
        payload = json.loads(result.content)
        assert payload == {"tool": "read_file", "arguments": {"path": "a.txt"}}
        assert connector.created[0].calls == [("read_file", {"path": "a.txt"})]
    finally:
        await client.stop()
        await bridge.stop()


def test_bridge_reports_tool_error_as_failed_result() -> None:
    """MCP 的 ``isError`` 变成 ``success=False``，错误文本进 ``error_msg``。"""
    asyncio.run(_reports_tool_error())


async def _reports_tool_error() -> None:
    hub = MemoryFanout()
    connector = FakeConnector(
        {"fs": _tools()},
        call=lambda _name, _args: McpCallResult(content="文件不存在", is_error=True),
    )
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "nope"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is False
        assert result.error_msg == "文件不存在"
    finally:
        await client.stop()
        await bridge.stop()


def test_bridge_returns_failure_on_timeout_without_dropping_session() -> None:
    """超时立刻回失败结果，且不因一次慢调用丢弃会话。"""
    asyncio.run(_handles_timeout())


async def _handles_timeout() -> None:
    hub = MemoryFanout()

    def _timeout(_name: str, _args: dict[str, object]) -> McpCallResult:
        raise McpTimeoutError("60s 内未响应")

    connector = FakeConnector({"fs": _tools()}, call=_timeout)
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "a.txt"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is False
        assert result.error_msg is not None
        assert "超时" in result.error_msg
        assert connector.created[0].closed is False
    finally:
        await client.stop()
        await bridge.stop()


def test_bridge_fails_immediately_when_server_is_down() -> None:
    """server 掉线时立即失败，绝不让调用等到 agent 的 tool_timeout。"""
    asyncio.run(_fails_when_down())


async def _fails_when_down() -> None:
    hub = MemoryFanout()

    def _broken(_name: str, _args: dict[str, object]) -> McpCallResult:
        raise McpTransportError("子进程已退出")

    connector = FakeConnector({"fs": _tools()}, call=_broken)
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "a.txt"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is False
        assert result.error_msg is not None
        assert "通信中断" in result.error_msg
    finally:
        await client.stop()
        await bridge.stop()


def test_bridge_ignores_calls_for_other_tools() -> None:
    """不属于本桥的工具调用直接忽略，不发布任何结果。"""
    asyncio.run(_ignores_foreign_calls())


async def _ignores_foreign_calls() -> None:
    from blue_contracts_tool import ToolCallRequest

    hub = MemoryFanout()
    bridge = _bridge(hub, _config(), FakeConnector({"fs": _tools()}))
    await bridge.start()
    proxy = RecordingProxy()
    try:
        await bridge.handle_call(
            ToolCallRequest(
                session_id="s", request_id="r", tool="clock.now", arguments={}
            ),
            cast(EventBus.Proxy, proxy),
        )
        assert proxy.published == []
    finally:
        await bridge.stop()


def test_bridge_fails_loud_when_declared_tool_is_missing() -> None:
    """声明了但 server 没有的工具 → 启动失败，而不是静默少一个。"""
    asyncio.run(_fails_on_missing_tool())


async def _fails_on_missing_tool() -> None:
    hub = MemoryFanout()
    connector = FakeConnector(
        {"fs": [McpToolInfo(name="other", description="另一个工具", input_schema={})]}
    )
    runtime = make_runtime(hub, "mcp_bridge")
    bridge = McpBridge(runtime=runtime, config=_config(), ttl=5.0, connector=connector)
    with pytest.raises(McpError) as captured:
        await bridge.start()
    assert TOOL_NAME in str(captured.value)
    assert runtime.is_running is False
    assert connector.created[0].closed is True


def test_bridge_requires_tool_whitelist_when_servers_declared() -> None:
    """声明了 server 却没有任何工具 → 配置自相矛盾，启动失败。"""
    asyncio.run(_fails_without_tools())


async def _fails_without_tools() -> None:
    hub = MemoryFanout()
    bridge = _bridge(hub, _config(tools=[]), FakeConnector({"fs": _tools()}))
    with pytest.raises(McpError) as captured:
        await bridge.start()
    assert "tools" in str(captured.value)


def test_bridge_propagates_connection_failure() -> None:
    """server 连不上时启动失败（由 supervisor 决定重启与退避）。"""
    asyncio.run(_fails_on_connect())


async def _fails_on_connect() -> None:
    hub = MemoryFanout()
    connector = FakeConnector({"fs": _tools()}, failures=1)
    bridge = _bridge(hub, _config(), connector)
    with pytest.raises(McpError):
        await bridge.start()
    assert connector.connects == ["fs"]


def test_bridge_runs_idle_when_all_servers_disabled() -> None:
    """全部 server 被禁用时以空目录运行，不影响其它工具。"""
    asyncio.run(_runs_idle())


async def _runs_idle() -> None:
    hub = MemoryFanout()
    config = McpBridgeConfig.model_validate(
        {"servers": [{"name": "fs", "command": "python", "enabled": False}]}
    )
    connector = FakeConnector({"fs": _tools()})
    bridge = _bridge(hub, config, connector)
    await bridge.start()
    try:
        assert bridge.tools == {}
        assert connector.connects == []
    finally:
        await bridge.stop()


def test_bridge_withdraws_leases_on_stop() -> None:
    """停机时注销全部租约，目录回到空。"""
    asyncio.run(_withdraws_on_stop())


async def _withdraws_on_stop() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    bridge = _bridge(hub, _config(), FakeConnector({"fs": _tools()}))
    await registry.start()
    await bridge.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)
    finally:
        await bridge.stop()
    try:
        await wait_until(lambda: registry.state.worker_count == 0)
    finally:
        await registry.stop()


def test_bridge_reconnects_dead_session() -> None:
    """会话失效后按需重连一次，调用仍然成功。"""
    asyncio.run(_reconnects())


async def _reconnects() -> None:
    hub = MemoryFanout()
    connector = FakeConnector({"fs": _tools()})
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        connector.created[0].alive = False
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "a.txt"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert connector.connects == ["fs", "fs"]
    finally:
        await client.stop()
        await bridge.stop()


def test_bridge_exposes_schema_degradation_in_description() -> None:
    """降级说明写进工具描述，模型与人都能看到。"""
    asyncio.run(_exposes_degradation())


async def _exposes_degradation() -> None:
    hub = MemoryFanout()
    connector = FakeConnector({"fs": [_broken_schema_tool()]})
    bridge = _bridge(hub, _config(), connector)
    await bridge.start()
    try:
        definition = bridge.tools[TOOL_NAME].definition
        assert "[blue] 参数 schema 已降级" in definition.function.description
        assert set(definition.function.parameters.properties) == {"paths"}
    finally:
        await bridge.stop()


def test_bridge_supervisor_reconnects_periodically() -> None:
    """巡检发现会话已死会自行重连（不依赖调用触发）。"""
    asyncio.run(_supervisor_reconnects())


async def _supervisor_reconnects() -> None:
    hub = MemoryFanout()
    connector = FakeConnector({"fs": _tools()})
    bridge = _bridge(hub, _config(), connector)
    await bridge.start()
    try:
        connector.created[0].alive = False
        await wait_until(lambda: len(connector.connects) == 2, timeout=3.0)
    finally:
        await bridge.stop()


def test_bridge_end_to_end_with_real_stdio_server() -> None:
    """端到端：真实 stdio 子进程 + 进程内总线 + 注册中心。

    这一条不替换连接器，走完「启动子进程 → 握手 → 翻页取目录 → 注册 →
    总线调用 → content 降级」的完整链路。
    """
    asyncio.run(_end_to_end())


async def _end_to_end() -> None:
    import sys
    from pathlib import Path

    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    config = McpBridgeConfig.model_validate(
        {
            "servers": [
                {"name": "fake", "command": sys.executable, "args": [str(fake)]},
            ],
            "tools": ["fake__echo", "fake__zod_schema"],
            "call_timeout": 5.0,
            "startup_timeout": 5.0,
            "health_interval": 5.0,
        }
    )
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    bridge = McpBridge(runtime=make_runtime(hub, "mcp_bridge"), config=config, ttl=5.0)
    client = make_runtime(hub, "client")
    await registry.start()
    await bridge.start()
    await client.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 2)
        catalog = await request(
            client.proxy(),
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        names = sorted(tool.function.name for tool in catalog.tools)
        assert names == ["fake__echo", "fake__zod_schema"]

        degraded = next(
            tool for tool in catalog.tools if tool.function.name == "fake__zod_schema"
        )
        assert "[blue] 参数 schema 已降级" in degraded.function.description

        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "fake__echo", "arguments": {"text": "端到端"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert "echo: 端到端" in result.content
        assert '"echoed": "端到端"' in result.content
    finally:
        await client.stop()
        await bridge.stop()
        await registry.stop()


def test_bridge_forwards_mcp_image_as_tool_attachment() -> None:
    """MCP 返回的图片块变成 tool.result.attachments（不再降级成占位文本）。"""
    asyncio.run(_forwards_image())


async def _forwards_image() -> None:
    hub = MemoryFanout()
    connector = FakeConnector(
        {"fs": _tools()},
        call=lambda _name, _args: McpCallResult(
            content="截图如下",
            is_error=False,
            attachments=(McpAttachment(mime="image/png", data="QUJDRA=="),),
        ),
    )
    bridge = _bridge(hub, _config(), connector)
    client = make_runtime(hub, "client")
    await bridge.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": TOOL_NAME, "arguments": {"path": "a.txt"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert [item.mime for item in result.attachments] == ["image/png"]
        assert result.attachments[0].data == "QUJDRA=="
        assert "截图如下" in result.content
    finally:
        await client.stop()
        await bridge.stop()
