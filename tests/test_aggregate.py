"""本体聚合测试：同一总线成员上同时提供会话与工具目录。"""

from __future__ import annotations

import asyncio

from handlers.tools.echo import echo
from infinity_agent import Message
from infinity_bus.templates.request import request

from blue.aggregate import AgentAggregate
from blue.config import AgentSettings, BlueSettings, RegistrySettings
from blue.protocol import CatalogQueryResponse, SessionReplyResponse, Topic, TopicBook
from blue.transport import MemoryFanout
from blue.worker import ToolWorker
from tests.support import ScriptedClient, make_runtime, wait_until


def test_aggregate_hosts_sessions_and_catalog() -> None:
    """聚合既应答目录查询，也处理会话消息。"""
    asyncio.run(_scenario())


async def _scenario() -> None:
    topics = TopicBook("test")
    hub = MemoryFanout()
    settings = BlueSettings(
        agent=AgentSettings(api_key_env="BLUE_UNUSED_KEY", max_steps=2),
        registry=RegistrySettings(ttl=5.0, sweep_interval=0.05),
    )
    aggregate = AgentAggregate(
        runtime=make_runtime(hub, "core", topics),
        settings=settings,
        client=ScriptedClient([Message.assistant(content="聚合可用")]),
    )
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.echo", topics), tool=echo, ttl=5.0
    )
    ui = make_runtime(hub, "ui", topics)

    await aggregate.start()
    await worker.start()
    await ui.start()
    try:
        await wait_until(lambda: aggregate.registry.state.worker_count == 1)

        catalog = await request(
            ui.proxy(),
            topics.name(Topic.CATALOG_QUERY),
            {},
            topics.name(Topic.CATALOG_REPLY),
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        assert [tool.function.name for tool in catalog.tools] == ["echo"]

        reply = await request(
            ui.proxy(),
            topics.name(Topic.SESSION_PROMPT),
            {"text": "你好"},
            topics.name(Topic.SESSION_REPLY),
            session_id="s-1",
            timeout=10.0,
        )
        assert isinstance(reply, SessionReplyResponse)
        assert reply.text == "聚合可用"
        assert aggregate.is_running
    finally:
        await ui.stop()
        await worker.stop()
        await aggregate.stop()

    assert not aggregate.is_running
