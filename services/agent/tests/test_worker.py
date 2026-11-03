"""工具进程测试：通过进程内总线注册、发现并调用工具。"""

from __future__ import annotations

import asyncio
import json

from blue_service_agent.catalog import ToolRegistryService
from blue_service_echo import echo
from blue_service_shell import run_shell
from blue_service_text_stats import text_stats
from infinity_bus.templates.request import request
from tests.support import make_runtime, wait_until

from blue.protocol import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from blue.transport import MemoryFanout
from blue.worker import ToolWorker


def test_worker_registers_and_serves_calls() -> None:
    """工具进程能登记到注册中心并通过总线返回结果。"""
    asyncio.run(_worker_serves_calls())


async def _worker_serves_calls() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"),
        ttl=5.0,
        sweep_interval=0.05,
    )
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)
    client = make_runtime(hub, "client")
    await registry.start()
    await worker.start()
    await client.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)

        catalog = await request(
            client.proxy(),
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        assert [tool.function.name for tool in catalog.tools] == [
            worker.definition.function.name
        ]

        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "echo", "arguments": {"text": "ping"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert json.loads(result.content) == {"text": "ping", "length": 4}
    finally:
        await client.stop()
        await worker.stop()
        await registry.stop()


def test_worker_reports_validation_errors() -> None:
    """参数校验失败时返回失败响应而不是抛异常。"""
    asyncio.run(_worker_reports_errors())


async def _worker_reports_errors() -> None:
    hub = MemoryFanout()
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.stats"), tool=text_stats, ttl=5.0
    )
    client = make_runtime(hub, "client")
    await worker.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "text_stats", "arguments": {}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is False
        assert result.error_msg is not None
    finally:
        await client.stop()
        await worker.stop()


def test_worker_serves_async_tool() -> None:
    """异步工具经 ToolWorker 等待执行并返回结果。"""
    asyncio.run(_worker_serves_async_tool())


async def _worker_serves_async_tool() -> None:
    hub = MemoryFanout()
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.shell"), tool=run_shell, ttl=5.0
    )
    client = make_runtime(hub, "client")
    await worker.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "run_shell", "arguments": {"command": "printf ping"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        payload = json.loads(result.content)
        assert payload["stdout"] == "ping"
        assert payload["exit_code"] == 0
        assert payload["timed_out"] is False
    finally:
        await client.stop()
        await worker.stop()
