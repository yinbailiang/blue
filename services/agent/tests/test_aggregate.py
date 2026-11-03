"""本体聚合测试：同一总线成员上同时提供会话与工具目录。"""

from __future__ import annotations

import asyncio

from blue_contracts_session import (
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionSnapshot,
)
from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
)
from blue_service_agent.aggregate import AgentAggregate
from blue_service_agent.config import AgentConfig
from blue_service_echo import echo
from blue_service_registry import ToolRegistryService
from infinity_agent import Message
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import ScriptedClient, make_runtime, wait_until

from blue.messages import encode_messages
from blue.transport import MemoryFanout
from blue.worker import ToolWorker


class _MemorySessionGateway:
    """测试用内存会话网关。"""

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def ensure(
        self, bus_proxy: EventBus.Proxy, session_id: str, system_prompt: str
    ) -> SessionSnapshot:
        del bus_proxy
        stored = self._messages.setdefault(session_id, [Message.system(system_prompt)])
        return SessionSnapshot(
            session_id=session_id,
            messages=encode_messages(stored),
            created_at=0.0,
            updated_at=0.0,
            message_count=len(stored),
        )

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
    ) -> None:
        del bus_proxy
        self._messages.setdefault(session_id, []).extend(messages)

    async def update(
        self, bus_proxy: EventBus.Proxy, session_id: str, steps: int
    ) -> None:
        del bus_proxy, session_id, steps


def test_agent_and_registry_are_separate_services() -> None:
    """目录由独立 registry 应答，会话由 agent 应答。"""
    asyncio.run(_scenario())


async def _scenario() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"),
        ttl=5.0,
        sweep_interval=0.05,
    )
    aggregate = AgentAggregate(
        runtime=make_runtime(hub, "core"),
        agent=AgentConfig(api_key="sk-test", max_steps=2),
        client=ScriptedClient([Message.assistant(content="聚合可用")]),
        session_gateway=_MemorySessionGateway(),
    )
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)
    ui = make_runtime(hub, "ui")

    await registry.start()
    await aggregate.start()
    await worker.start()
    await ui.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)

        catalog = await request(
            ui.proxy(),
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=5.0,
        )
        assert isinstance(catalog, CatalogQueryResponse)
        assert [tool.function.name for tool in catalog.tools] == ["echo"]

        reply = await request(
            ui.proxy(),
            SessionPromptEvent.name,
            {"text": "你好"},
            SessionReplyEvent.name,
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
        await registry.stop()

    assert not aggregate.is_running
