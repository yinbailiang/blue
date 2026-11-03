"""Agent 核心测试：模型循环 + 总线工具调用 + 会话网关。"""

from __future__ import annotations

import asyncio

import pytest
from blue_service_agent.agent import AgentCore
from blue_service_agent.catalog import ToolRegistryService
from blue_service_agent.config import AgentConfig
from blue_service_echo import echo
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import request
from pydantic import BaseModel
from tests.support import ScriptedClient, make_runtime, wait_until

from blue.errors import RuntimeConfigError
from blue.messages import encode_messages
from blue.protocol import (
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionSnapshot,
    SessionStreamEvent,
    SessionStreamPayload,
)
from blue.transport import MemoryFanout
from blue.worker import ToolWorker


class StreamCollector(EventHandler):
    """收集 Agent 的 session.stream 事件。"""

    def __init__(self) -> None:
        super().__init__(
            subscriptions=[SessionStreamEvent.name],
            handle_timeout=1.0,
        )
        self.events: list[SessionStreamPayload] = []

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, SessionStreamPayload):
            self.events.append(payload)


class MemorySessionGateway:
    """测试用内存会话网关。"""

    def __init__(self) -> None:
        self.messages: dict[str, list[Message]] = {}
        self.steps: dict[str, int] = {}

    async def ensure(
        self, bus_proxy: EventBus.Proxy, session_id: str, system_prompt: str
    ) -> SessionSnapshot:
        del bus_proxy
        stored = self.messages.setdefault(session_id, [Message.system(system_prompt)])
        return SessionSnapshot(
            session_id=session_id,
            messages=encode_messages(stored),
            created_at=0.0,
            updated_at=0.0,
            steps=self.steps.get(session_id, 0),
            message_count=len(stored),
        )

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
    ) -> None:
        del bus_proxy
        self.messages.setdefault(session_id, []).extend(messages)

    async def update(
        self, bus_proxy: EventBus.Proxy, session_id: str, steps: int
    ) -> None:
        del bus_proxy
        self.steps[session_id] = steps


def _echo_call(call_id: str, text: str) -> ToolCall:
    """构造一次 echo 工具调用。"""
    return ToolCall(
        id=call_id, function=FunctionCall(name="echo", arguments={"text": text})
    )


def test_agent_calls_tool_then_replies() -> None:
    """模型发起工具调用 → 总线转发给工具进程 → 结果回灌 → 最终答复。"""
    asyncio.run(_agent_calls_tool())


async def _agent_calls_tool() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"),
        ttl=5.0,
        sweep_interval=0.05,
    )
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)
    client = ScriptedClient(
        [
            Message.assistant(tool_calls=[_echo_call("call-1", "ping")]),
            Message.assistant(content="已完成"),
        ]
    )
    gateway = MemorySessionGateway()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4),
        client=client,
        session_gateway=gateway,
    )
    chat = make_runtime(hub, "chat")
    collector = StreamCollector()
    collector_runtime = make_runtime(hub, "stream.collector")

    await registry.start()
    await worker.start()
    await core.start()
    await chat.start()
    await collector_runtime.start()
    await collector_runtime.register(collector)
    try:
        await wait_until(lambda: registry.state.worker_count == 1)
        response = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {"text": "帮我回显 ping"},
            SessionReplyEvent.name,
            session_id="session-1",
            timeout=10.0,
        )
        assert isinstance(response, SessionReplyResponse)
        assert response.success is True
        assert response.text == "已完成"
        assert response.steps == 2
    finally:
        await collector_runtime.stop()
        await chat.stop()
        await core.stop()
        await worker.stop()
        await registry.stop()

    kinds = [event.kind for event in collector.events]
    assert kinds[0] == "step_start"
    assert "tool_calls" in kinds
    assert "tool_start" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "done"
    assert any(
        event.kind == "text" and event.text == "已完成" for event in collector.events
    )

    session = gateway.messages["session-1"]
    assert [message.role for message in session] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    tool_message = session[3]
    assert tool_message.tool_call_id == "call-1"
    assert isinstance(tool_message.content, str)
    assert "ping" in tool_message.content


def test_agent_stops_at_max_steps() -> None:
    """模型持续调用工具时，循环在最大步数处终止并给出提示。"""
    asyncio.run(_agent_stops_at_max_steps())


async def _agent_stops_at_max_steps() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"),
        ttl=5.0,
        sweep_interval=0.05,
    )
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)
    client = ScriptedClient(
        [Message.assistant(tool_calls=[_echo_call("loop-1", "x")]) for _ in range(5)]
    )
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=2),
        client=client,
        session_gateway=MemorySessionGateway(),
    )
    chat = make_runtime(hub, "chat")

    await registry.start()
    await worker.start()
    await core.start()
    await chat.start()
    try:
        await wait_until(lambda: registry.state.worker_count == 1)
        response = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {"text": "一直调用工具"},
            SessionReplyEvent.name,
            session_id="session-2",
            timeout=10.0,
        )
        assert isinstance(response, SessionReplyResponse)
        assert response.steps == 2
        assert "最大步数" in response.text
        assert len(client.calls) == 2
    finally:
        await chat.stop()
        await core.stop()
        await worker.stop()
        await registry.stop()


def test_resolve_api_key_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置密钥时报错，且不再回退到环境变量。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")

    with pytest.raises(RuntimeConfigError, match=r"secrets\.toml"):
        AgentConfig().resolve_api_key()


def test_resolve_api_key_uses_configured_value() -> None:
    """配置了密钥就使用配置值。"""
    assert AgentConfig(api_key="sk-test").resolve_api_key() == "sk-test"
