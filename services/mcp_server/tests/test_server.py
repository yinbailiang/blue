"""服务端桥测试：用内存通道驱动完整 JSON-RPC 会话。

不启子进程：``McpStdioServer.serve`` 只依赖 ``MessageIO``，测试用脚本化的内存通道
喂请求、收响应；工具侧用真实 ``ToolWorker`` + 进程内总线，因此覆盖了
``catalog.query`` / ``tool.call`` / ``tool.result`` 的真实链路。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import pytest
from blue_contracts_tool import ToolDefinition
from blue_service_mcp_server import McpServerSettings, McpStdioServer
from blue_service_mcp_server.server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from blue_service_registry import ToolRegistryService
from infinity_agent.tools import ToolRegistry
from tests.support import make_runtime, wait_until

from blue.transport import BusRuntime, MemoryFanout
from blue_tool_sdk import ToolLease, ToolRegistrar, ToolWorker


class FakeIO:
    """按脚本推送请求、收集响应的内存通道。"""

    def __init__(self, lines: list[str]) -> None:
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        for line in lines:
            self._inbox.put_nowait(line)
        self._inbox.put_nowait(None)
        self.written: list[dict[str, Any]] = []

    async def read_line(self) -> str | None:
        """取下一条脚本行；末尾返回 EOF。"""
        return await self._inbox.get()

    async def write_line(self, text: str) -> None:
        """记录一条响应（顺便校验是合法 JSON）。"""
        self.written.append(json.loads(text))


def echo(text: Annotated[str, "要回显的文本"]) -> str:
    """原样回显。"""
    return text


def fail(reason: Annotated[str, "失败原因"]) -> str:
    """总是失败。"""
    raise ValueError(reason)


async def slow(seconds: Annotated[float, "睡眠秒数"] = 0.3) -> str:
    """睡眠后返回。"""
    await asyncio.sleep(seconds)
    return "done"


def _request(request_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    """拼一条 JSON-RPC 请求。"""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, ensure_ascii=False)


def _by_id(written: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """把响应按 id 建索引。"""
    return {
        int(message["id"]): message
        for message in written
        if isinstance(message.get("id"), int)
    }


class Harness:
    """注册中心 + 若干工具进程 + 待测 server 的临时环境。"""

    def __init__(
        self,
        *,
        expose: list[str],
        timeout: float = 5.0,
        tools: tuple[Any, ...] = (echo, fail, slow),
    ) -> None:
        self.hub = MemoryFanout()
        self.registry = ToolRegistryService(
            runtime=make_runtime(self.hub, "registry"), ttl=5.0, sweep_interval=0.05
        )
        self.workers = [
            ToolWorker(
                runtime=make_runtime(self.hub, f"tool.{tool.__name__}"),
                tool=tool,
                ttl=5.0,
            )
            for tool in tools
        ]
        self.runtime: BusRuntime = make_runtime(self.hub, "ui.mcp_server")
        self.server = McpStdioServer(
            runtime=self.runtime,
            settings=McpServerSettings(expose=expose, timeout=timeout),
        )

    async def __aenter__(self) -> Harness:
        """启动注册中心与全部工具进程。"""
        await self.registry.start()
        for worker in self.workers:
            await worker.start()
        if self.workers:
            await wait_until(
                lambda: self.registry.state.worker_count == len(self.workers)
            )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """停掉工具进程与注册中心。"""
        for worker in self.workers:
            await worker.stop()
        await self.registry.stop()

    async def serve(self, lines: list[str]) -> list[dict[str, Any]]:
        """喂脚本、跑到 EOF、返回全部响应。"""
        io = FakeIO(lines)
        await self.server.serve(io)
        return io.written


def test_initialize_echoes_protocol_and_reports_server_info() -> None:
    """握手回显客户端协议版本，并声明不发送 listChanged。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve(
                [
                    _request(
                        1,
                        "initialize",
                        {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "claude-code", "version": "1.0"},
                        },
                    ),
                    '{"jsonrpc":"2.0","method":"notifications/initialized"}',
                ]
            )
        assert len(written) == 1
        result = _by_id(written)[1]["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert result["capabilities"]["tools"]["listChanged"] is False
        assert result["serverInfo"]["name"] == "blue"

    asyncio.run(case())


def test_lists_only_exposed_tools_with_schema() -> None:
    """``tools/list`` 只返回白名单内工具，并把参数转成 JSON Schema。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve([_request(1, "tools/list")])
        tools = _by_id(written)[1]["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["echo"]
        schema = tools[0]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["required"] == ["text"]
        assert schema["properties"]["text"]["type"] == "string"
        assert schema["properties"]["text"]["description"] == "要回显的文本"

    asyncio.run(case())


def test_calls_tool_and_maps_result() -> None:
    """``tools/call`` 走 ``tool.call``，成功结果映射成 text 块 + isError=false。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve(
                [
                    _request(
                        1, "tools/call", {"name": "echo", "arguments": {"text": "hi"}}
                    )
                ]
            )
        result = _by_id(written)[1]["result"]
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "hi"}]

    asyncio.run(case())


def test_tool_failure_becomes_is_error() -> None:
    """工具抛错 → ``isError: true``，错误文本进 content。"""

    async def case() -> None:
        async with Harness(expose=["fail"]) as harness:
            written = await harness.serve(
                [
                    _request(
                        1,
                        "tools/call",
                        {"name": "fail", "arguments": {"reason": "炸了"}},
                    )
                ]
            )
        result = _by_id(written)[1]["result"]
        assert result["isError"] is True
        assert "炸了" in result["content"][0]["text"]

    asyncio.run(case())


def test_unexposed_tool_is_rejected() -> None:
    """目录里有但不在白名单里的工具一律拒绝。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve(
                [
                    _request(
                        1, "tools/call", {"name": "fail", "arguments": {"reason": "x"}}
                    )
                ]
            )
        error = _by_id(written)[1]["error"]
        assert error["code"] == INVALID_PARAMS
        assert "白名单" in error["message"]

    asyncio.run(case())


def test_whitelisted_but_missing_tool_fails_fast() -> None:
    """白名单里有、目录里没有的工具立即失败（不等到超时）。"""

    async def case() -> None:
        async with Harness(expose=["ghost"], timeout=30.0) as harness:
            written = await harness.serve(
                [_request(1, "tools/call", {"name": "ghost", "arguments": {}})]
            )
        error = _by_id(written)[1]["error"]
        assert error["code"] == INVALID_PARAMS
        assert "不在目录中" in error["message"]

    asyncio.run(case())


def test_ghost_tool_name_is_only_logged_in_listing() -> None:
    """``tools/list`` 对「白名单有、目录无」只告警，不报错。"""

    async def case() -> None:
        async with Harness(expose=["ghost"]) as harness:
            written = await harness.serve([_request(1, "tools/list")])
        assert _by_id(written)[1]["result"]["tools"] == []

    asyncio.run(case())


def test_missing_tool_name_is_invalid_params() -> None:
    """``tools/call`` 缺工具名 → -32602。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve(
                [_request(1, "tools/call", {"arguments": {}})]
            )
        assert _by_id(written)[1]["error"]["code"] == INVALID_PARAMS

    asyncio.run(case())


def test_unknown_method_is_method_not_found() -> None:
    """未实现的方法（如 resources/list）→ -32601。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve([_request(1, "resources/list")])
        error = _by_id(written)[1]["error"]
        assert error["code"] == METHOD_NOT_FOUND
        assert "resources/list" in error["message"]

    asyncio.run(case())


def test_ping_returns_empty_result() -> None:
    """``ping`` 回空结果，用于活性检查。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve([_request(1, "ping")])
        assert _by_id(written)[1]["result"] == {}

    asyncio.run(case())


def test_parse_error_reports_null_id() -> None:
    """非 JSON 行 → -32700，id 为 null。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            written = await harness.serve(["这不是 JSON"])
        assert written[0]["id"] is None
        assert written[0]["error"]["code"] == PARSE_ERROR

    asyncio.run(case())


def test_call_times_out_when_tool_is_registered_but_silent() -> None:
    """租约还在、但没人应答的调用超时后返回错误（模拟卡死的工具服务）。"""

    async def case() -> None:
        hub = MemoryFanout()
        registry = ToolRegistryService(
            runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
        )
        catalog = ToolRegistry()
        catalog.register(echo)
        definition: ToolDefinition = catalog.definitions[0]
        ghost_runtime = make_runtime(hub, "tool.echo")
        ghost = ToolRegistrar(
            runtime=ghost_runtime,
            leases=[ToolLease(worker_id="tool.echo.ghost", definition=definition)],
            ttl=5.0,
        )
        server = McpStdioServer(
            runtime=make_runtime(hub, "ui.mcp_server"),
            settings=McpServerSettings(expose=["echo"], timeout=0.2),
        )
        io = FakeIO(
            [_request(1, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})]
        )
        await registry.start()
        await ghost_runtime.start()
        await ghost.announce()
        try:
            await wait_until(lambda: registry.state.worker_count == 1)
            await server.serve(io)
        finally:
            await ghost.withdraw()
            await ghost_runtime.stop()
            await registry.stop()
        error = _by_id(io.written)[1]["error"]
        assert error["code"] == INTERNAL_ERROR
        assert "没有响应" in error["message"]

    asyncio.run(case())


def test_slow_call_does_not_block_ping() -> None:
    """请求并发处理：慢工具调用期间 ``ping`` 照常返回。"""

    async def case() -> None:
        async with Harness(expose=["slow"]) as harness:
            written = await harness.serve(
                [
                    _request(
                        1, "tools/call", {"name": "slow", "arguments": {"seconds": 0.3}}
                    ),
                    _request(2, "ping"),
                ]
            )
        assert [message["id"] for message in written] == [2, 1]
        assert _by_id(written)[1]["result"]["content"][0]["text"] == "done"

    asyncio.run(case())


def test_runtime_stops_after_eof() -> None:
    """``serve`` 结束后总线运行时已关闭，进程可干净退出。"""

    async def case() -> None:
        async with Harness(expose=["echo"]) as harness:
            await harness.serve([_request(1, "ping")])
            assert harness.runtime.is_running is False

    asyncio.run(case())


def test_exposed_order_follows_config() -> None:
    """``exposed`` 暴露配置顺序，``session_id`` 每次进程唯一。"""

    async def case() -> None:
        async with Harness(expose=["slow", "echo"], tools=()) as harness:
            assert harness.server.exposed == ("slow", "echo")
            assert harness.server.session_id.startswith("mcp-server.")

    asyncio.run(case())


def test_settings_reject_empty_expose() -> None:
    """空白名单在配置期就失败（不支持「全部」）。"""
    with pytest.raises(ValueError):
        McpServerSettings(expose=[])
