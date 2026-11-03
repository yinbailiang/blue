"""跨进程集成测试：需要可达的 RabbitMQ broker（不可达时自动跳过）。

手动运行方式::

    rabbitmq-server &
    BLUE_RABBIT_URL=amqp://guest:guest@127.0.0.1:5672/ \\
        uv run pytest tests/test_integration_rabbit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from blue_contracts_tool import (
    EVENTS as TOOL_EVENTS,
)
from blue_contracts_tool import (
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from blue_service_echo import echo
from blue_service_registry import ToolRegistryService
from infinity_bus.templates.request import request

from blue.transport import RabbitRuntime
from blue.worker import ToolWorker

RABBIT_URL = os.environ.get(
    "BLUE_RABBIT_URL",
    os.environ.get("BLUE_BUS_URL", "amqp://guest:guest@127.0.0.1:5672/blue"),
)


def _broker_reachable(url: str) -> bool:
    """探测 broker 端口是否可连接。"""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5672
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _broker_reachable(RABBIT_URL), reason=f"RabbitMQ 不可达：{RABBIT_URL}"
)


def test_tool_registry_and_call_over_rabbitmq() -> None:
    """注册中心、工具进程与调用方通过 RabbitMQ fanout 跨进程互通。"""
    asyncio.run(_rabbit_roundtrip())


async def _rabbit_roundtrip() -> None:
    suffix = uuid.uuid4().hex[:8]
    registry_runtime = RabbitRuntime(
        url=RABBIT_URL,
        member_id=f"registry.{suffix}",
        events=TOOL_EVENTS,
    )
    worker_runtime = RabbitRuntime(
        url=RABBIT_URL,
        member_id=f"tool.{suffix}",
        events=TOOL_EVENTS,
    )
    client_runtime = RabbitRuntime(
        url=RABBIT_URL,
        member_id=f"client.{suffix}",
        events=TOOL_EVENTS,
    )
    registry = ToolRegistryService(
        runtime=registry_runtime, ttl=5.0, sweep_interval=0.5
    )
    worker = ToolWorker(runtime=worker_runtime, tool=echo, ttl=5.0)
    await registry.start()
    await worker.start()
    await client_runtime.start()
    try:
        result = await request(
            client_runtime.proxy(),
            ToolCallEvent.name,
            {"tool": "echo", "arguments": {"text": "rabbit"}},
            ToolResultEvent.name,
            timeout=10.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert json.loads(result.content) == {"text": "rabbit", "length": 6}
    finally:
        await client_runtime.stop()
        await worker.stop()
        await registry.stop()
