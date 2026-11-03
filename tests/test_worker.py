"""工具进程测试：通过进程内总线注册、发现并调用工具。"""

from __future__ import annotations

import asyncio
import json

from handlers.tools.echo import echo
from handlers.tools.text import text_stats
from infinity_bus.templates.request import request

from blue.catalog import ToolRegistryService
from blue.protocol import (
    CatalogQueryResponse,
    ToolCallResult,
    Topic,
    TopicBook,
)
from blue.transport import MemoryFanout
from blue.worker import ToolWorker
from tests.support import make_runtime, wait_until


def test_worker_registers_and_serves_calls() -> None:
    """工具进程能登记到注册中心并通过总线返回结果。"""
    asyncio.run(_worker_serves_calls())


async def _worker_serves_calls() -> None:
    hub_topics = TopicBook("test")

    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry", hub_topics),
        ttl=5.0,
        sweep_interval=0.05,
    )
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.echo", hub_topics), tool=echo, ttl=5.0
    )
    client = make_runtime(hub, "client", hub_topics)
    await registry.start()
    await worker.start()
    await client.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)

        catalog = await request(
            client.proxy(),
            hub_topics.name(Topic.CATALOG_QUERY),
            {},
            hub_topics.name(Topic.CATALOG_REPLY),
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        assert [tool.function.name for tool in catalog.tools] == [
            worker.definition.function.name
        ]

        result = await request(
            client.proxy(),
            hub_topics.name(Topic.TOOL_CALL),
            {"tool": "echo", "arguments": {"text": "ping"}},
            hub_topics.name(Topic.TOOL_RESULT),
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
    topics = TopicBook("test")
    hub = MemoryFanout()
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.stats", topics), tool=text_stats, ttl=5.0
    )
    client = make_runtime(hub, "client", topics)
    await worker.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            topics.name(Topic.TOOL_CALL),
            {"tool": "text_stats", "arguments": {}},
            topics.name(Topic.TOOL_RESULT),
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is False
        assert result.error_msg is not None
    finally:
        await client.stop()
        await worker.stop()
