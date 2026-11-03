"""Agent 核心测试：会话中心 + 模型循环 + 总线工具调用。"""

from __future__ import annotations

import asyncio

from handlers.tools.echo import echo
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall
from infinity_bus.templates.request import request

from blue.agent import AgentCore
from blue.catalog import ToolRegistryService
from blue.config import AgentSettings
from blue.protocol import SessionPromptEvent, SessionReplyEvent, SessionReplyResponse
from blue.transport import MemoryFanout
from blue.worker import ToolWorker
from tests.support import ScriptedClient, make_runtime, wait_until


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
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentSettings(api_key_env="BLUE_UNUSED_KEY", max_steps=4),
        client=client,
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
        await chat.stop()
        await core.stop()
        await worker.stop()
        await registry.stop()

    session = core.sessions.sessions["session-1"]
    assert [message.role for message in session.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    tool_message = session.messages[3]
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
        settings=AgentSettings(api_key_env="BLUE_UNUSED_KEY", max_steps=2),
        client=client,
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
