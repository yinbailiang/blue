"""Agent 核心测试：模型循环 + 总线工具调用 + 会话网关。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from blue_contracts_session import (
    SessionActivityEvent,
    SessionActivityPayload,
    SessionActivityQueryEvent,
    SessionActivityQueryResponse,
    SessionActivityReplyEvent,
    SessionPromptEvent,
    SessionPromptRequest,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionStreamEvent,
    SessionStreamPayload,
)
from blue_service_agent.agent import AgentCore, build_client
from blue_service_agent.config import AgentConfig
from blue_service_echo import echo
from blue_service_registry import ToolRegistryService
from infinity_agent import (
    FunctionCall,
    Message,
    MessageRole,
    Messages,
    StreamChunk,
    TextChunk,
    ToolCall,
    ToolDefinition,
)
from infinity_agent.clients import RequestConfig
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import request
from pydantic import BaseModel
from tests.support import (
    MemorySessionGateway,
    ScriptedClient,
    make_runtime,
    wait_until,
)

from blue.errors import RuntimeConfigError
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


class ActivityCollector(EventHandler):
    """收集 Agent 广播的 session.activity 事件。"""

    def __init__(self) -> None:
        super().__init__(
            subscriptions=[SessionActivityEvent.name],
            handle_timeout=1.0,
        )
        self.events: list[SessionActivityPayload] = []

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, SessionActivityPayload):
            self.events.append(payload)


class StubCatalog:
    """不查总线的空工具目录（避免测试里等 5 秒目录超时）。"""

    def __init__(self) -> None:
        self.tools: list[ToolDefinition] = []

    async def start(self) -> None:
        return

    async def refresh(self, bus_proxy: EventBus.Proxy) -> list[ToolDefinition]:
        del bus_proxy
        return []


class FlakyClient(ScriptedClient):
    """前 N 次流式请求直接抛网络错误，之后正常。"""

    def __init__(self, script: list[Message], *, failures: int = 1) -> None:
        super().__init__(script)
        self._failures = failures
        self.attempts = 0

    async def stream_chat(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        messages: Messages,
        config: RequestConfig | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        self.attempts += 1
        if self._failures > 0:
            self._failures -= 1
            raise ConnectionError("Timeout on reading data from socket")
        async for chunk in super().stream_chat(messages, config):
            yield chunk


class PartialThenFailClient(ScriptedClient):
    """先吐一段文本，再抛网络错误（模拟读到一半断开）。"""

    def __init__(self) -> None:
        super().__init__([])
        self.attempts = 0

    async def stream_chat(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        messages: Messages,
        config: RequestConfig | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        del messages, config
        self.attempts += 1
        yield TextChunk(text="已经生成的部分")
        raise ConnectionError("Timeout on reading data from socket")


class GatedClient(ScriptedClient):
    """第一轮流式响应先卡在闸门上，便于观察「进行中」状态。"""

    def __init__(self, script: list[Message]) -> None:
        super().__init__(script)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_chat(
        self,
        messages: Messages,
        config: RequestConfig | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        self.started.set()
        await self.release.wait()
        async for chunk in super().stream_chat(messages, config):
            yield chunk


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


def test_agent_rejects_unknown_tool_without_waiting() -> None:
    """模型请求不存在的工具时立即回灌错误，不再等满 tool_timeout。"""
    asyncio.run(_agent_rejects_unknown_tool())


async def _agent_rejects_unknown_tool() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"),
        ttl=5.0,
        sweep_interval=0.05,
    )
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)
    ghost = ToolCall(
        id="call-ghost",
        function=FunctionCall(name="shell_placeholder", arguments={"command": "ls"}),
    )
    client = ScriptedClient(
        [
            Message.assistant(tool_calls=[ghost]),
            Message.assistant(content="改用可用工具"),
        ]
    )
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4, tool_timeout=600.0),
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
        response = await asyncio.wait_for(
            request(
                chat.proxy(),
                SessionPromptEvent.name,
                {"text": "用 shell_placeholder 跑一下"},
                SessionReplyEvent.name,
                session_id="session-ghost",
                timeout=30.0,
            ),
            timeout=30.0,
        )
        assert isinstance(response, SessionReplyResponse)
        assert response.success is True
        assert response.text == "改用可用工具"
    finally:
        await chat.stop()
        await core.stop()
        await worker.stop()
        await registry.stop()

    tool_messages = [
        message for message in client.calls[-1] if message.role == MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call-ghost"
    assert isinstance(tool_messages[0].content, str)
    assert "不存在" in tool_messages[0].content
    assert "echo" in tool_messages[0].content


def test_agent_repairs_dangling_tool_call_from_history() -> None:
    """历史上遗留的悬空 tool_calls 在发送给模型前被补成可接受的消息序列。"""
    asyncio.run(_agent_repairs_dangling_tool_call_from_history())


async def _agent_repairs_dangling_tool_call_from_history() -> None:
    hub = MemoryFanout()
    gateway = MemorySessionGateway()
    gateway.seed(
        "session-broken",
        [
            Message.system("sys"),
            Message.user("去查一下"),
            Message.assistant(
                tool_calls=[
                    ToolCall(
                        id="call-lost",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ]
            ),
        ],
    )
    client = ScriptedClient([Message.assistant(content="继续完成")])
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4),
        client=client,
        session_gateway=gateway,
    )
    chat = make_runtime(hub, "chat")

    await core.start()
    await chat.start()
    try:
        response = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {"text": "继续"},
            SessionReplyEvent.name,
            session_id="session-broken",
            timeout=10.0,
        )
        assert isinstance(response, SessionReplyResponse)
        assert response.success is True
        assert response.text == "继续完成"
    finally:
        await chat.stop()
        await core.stop()

    sent = client.calls[0]
    assert [message.role for message in sent] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.USER,
    ]
    placeholder = sent[3]
    assert placeholder.tool_call_id == "call-lost"
    assert isinstance(placeholder.content, str)
    assert "call-lost" not in placeholder.content
    assert "echo" in placeholder.content


def test_cancel_without_active_turn_returns_false() -> None:
    """没有进行中的 turn 时，cancel() 直接返回 False。"""
    core = AgentCore(
        runtime=make_runtime(MemoryFanout(), "agent"),
        settings=AgentConfig(api_key="sk-test"),
        client=ScriptedClient([]),
    )

    assert core.cancel("no-such-session") is False


def test_cancel_interrupts_active_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel() 会取消该会话正在运行的 turn。"""
    asyncio.run(_cancel_active_turn(monkeypatch))


async def _cancel_active_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    core = AgentCore(
        runtime=make_runtime(MemoryFanout(), "agent"),
        settings=AgentConfig(api_key="sk-test"),
        client=ScriptedClient([]),
    )
    started = asyncio.Event()

    async def blocking_run(
        payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
    ) -> SessionReplyResponse:
        del bus_proxy
        started.set()
        await asyncio.sleep(30)
        return SessionReplyResponse(
            session_id=payload.session_id,
            request_id=payload.request_id,
        )

    monkeypatch.setattr(core, "_run_prompt", blocking_run)

    payload = SessionPromptRequest(session_id="s1", request_id="req", text="hi")
    task = asyncio.create_task(core.handle_prompt(payload, cast(EventBus.Proxy, None)))
    await started.wait()

    assert core.cancel("s1") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.cancel("s1") is False


def test_activity_tracks_running_session() -> None:
    """turn 进行中能被查询/广播，结束后自动清空。"""
    asyncio.run(_activity_tracks_running_session())


async def _activity_tracks_running_session() -> None:
    hub = MemoryFanout()
    client = GatedClient([Message.assistant(content="好了")])
    gateway = MemorySessionGateway()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4),
        client=client,
        session_gateway=gateway,
        catalog=cast(Any, StubCatalog()),
    )
    chat = make_runtime(hub, "chat")
    collector = ActivityCollector()
    collector_runtime = make_runtime(hub, "activity.collector")

    await core.start()
    await chat.start()
    await collector_runtime.start()
    await collector_runtime.register(collector)
    try:
        assert core.activity() == []
        prompt = asyncio.create_task(
            request(
                chat.proxy(),
                SessionPromptEvent.name,
                {"text": "在吗"},
                SessionReplyEvent.name,
                session_id="s-busy",
                timeout=10.0,
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=5.0)
        await wait_until(lambda: bool(core.activity()))

        running = core.activity()
        assert [item.session_id for item in running] == ["s-busy"]
        assert running[0].running is True
        assert running[0].phase in {"thinking", "streaming"}

        # 另一台设备/新页面靠这个 RPC 补上「谁在跑」
        snapshot = await request(
            chat.proxy(),
            SessionActivityQueryEvent.name,
            {},
            SessionActivityReplyEvent.name,
            session_id="other-ui",
            timeout=5.0,
        )
        assert isinstance(snapshot, SessionActivityQueryResponse)
        assert [item.session_id for item in snapshot.running] == ["s-busy"]

        client.release.set()
        response = await prompt
        assert isinstance(response, SessionReplyResponse)
        assert response.success is True
        assert core.activity() == []
        await wait_until(
            lambda: any(not item.activity.running for item in collector.events)
        )
    finally:
        await collector_runtime.stop()
        await chat.stop()
        await core.stop()

    assert collector.events[0].activity.running is True
    assert collector.events[0].activity.session_id == "s-busy"
    assert collector.events[-1].activity.running is False


def test_retries_stream_when_nothing_received() -> None:
    """还没收到任何内容就流中断：自动重试一次，用户看到正常回复。"""
    asyncio.run(_retries_stream())


async def _retries_stream() -> None:
    hub = MemoryFanout()
    client = FlakyClient([Message.assistant(content="重试后的回答")], failures=1)
    gateway = MemorySessionGateway()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4, llm_retries=1),
        client=client,
        session_gateway=gateway,
        catalog=cast(Any, StubCatalog()),
    )
    chat = make_runtime(hub, "chat")
    await core.start()
    await chat.start()
    try:
        response = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {"text": "在吗"},
            SessionReplyEvent.name,
            session_id="s-retry",
            timeout=10.0,
        )
    finally:
        await chat.stop()
        await core.stop()

    assert isinstance(response, SessionReplyResponse)
    assert response.success is True
    assert response.text == "重试后的回答"
    assert client.attempts == 2
    # 只落库一次，没有重复消息
    roles = [message.role for message in gateway.messages["s-retry"]]
    assert roles == [MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT]


def test_keeps_partial_reply_when_stream_drops_midway() -> None:
    """已经流出内容后中断：不重试，把那部分落库，而不是整轮丢掉。"""
    asyncio.run(_keeps_partial())


async def _keeps_partial() -> None:
    hub = MemoryFanout()
    client = PartialThenFailClient()
    gateway = MemorySessionGateway()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4, llm_retries=3),
        client=client,
        session_gateway=gateway,
        catalog=cast(Any, StubCatalog()),
    )
    chat = make_runtime(hub, "chat")
    await core.start()
    await chat.start()
    try:
        response = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {"text": "讲个长的"},
            SessionReplyEvent.name,
            session_id="s-partial",
            timeout=10.0,
        )
    finally:
        await chat.stop()
        await core.stop()

    assert isinstance(response, SessionReplyResponse)
    assert response.success is False
    assert client.attempts == 1  # 有内容就不重试
    stored = gateway.messages["s-partial"]
    assistant = stored[-1]
    assert assistant.role is MessageRole.ASSISTANT
    assert isinstance(assistant.content, str)
    assert "已经生成的部分" in assistant.content


def test_build_client_passes_timeout_and_retry_settings() -> None:
    """llm_* 配置会透传到底层客户端的连接配置（deepseek / openai 都一致）。"""
    for provider, model in (("deepseek", "deepseek-chat"), ("openai", "gpt-4o")):
        settings = AgentConfig(
            api_key="sk-test",
            provider=cast(Any, provider),
            model=model,
            llm_timeout=123.0,
            llm_connect_timeout=4.0,
            llm_http_retries=1,
        )
        client = build_client(settings)
        connection = getattr(getattr(client, "_config", None), "connection", None)
        assert getattr(connection, "timeout", None) == 123.0
        assert getattr(connection, "connect_timeout", None) == 4.0
        assert getattr(connection, "max_retries", None) == 1


def test_agent_config_defaults_are_generous_for_long_replies() -> None:
    """默认读超时必须明显大于库的 60s，否则长回答会被截断。"""
    config = AgentConfig(api_key="sk-test")

    assert config.llm_timeout == 300.0
    assert config.llm_retries == 1
    assert config.llm_connect_timeout == 10.0
    assert config.llm_http_retries == 3


def test_queue_timeout_rejects_when_session_busy() -> None:
    """同一会话排队超时：后来的请求返回「会话繁忙」，先来的照常完成。"""
    asyncio.run(_queue_timeout())


async def _queue_timeout() -> None:
    hub = MemoryFanout()
    client = GatedClient([Message.assistant(content="好了")])
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", queue_timeout=0.2),
        client=client,
        session_gateway=MemorySessionGateway(),
        catalog=cast(Any, StubCatalog()),
    )
    chat = make_runtime(hub, "chat")
    await core.start()
    await chat.start()
    try:
        proxy = chat.proxy()
        first = asyncio.create_task(
            core.handle_prompt(
                SessionPromptRequest(session_id="q", request_id="a", text="hi"), proxy
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=5.0)

        second = await core.handle_prompt(
            SessionPromptRequest(session_id="q", request_id="b", text="again"), proxy
        )
        assert second.success is False
        assert "繁忙" in (second.error_msg or "")

        client.release.set()
        assert (await first).success is True
    finally:
        await chat.stop()
        await core.stop()


def test_queue_event_announces_waiting() -> None:
    """入队失败时退回「排队等待」，并推送 kind=queued 事件给前端。"""
    asyncio.run(_queue_event())


async def _queue_event() -> None:
    hub = MemoryFanout()
    client = GatedClient([Message.assistant(content="好了")])
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", queue_timeout=2.0),
        client=client,
        session_gateway=MemorySessionGateway(),
        catalog=cast(Any, StubCatalog()),
    )
    chat = make_runtime(hub, "chat")
    await core.start()
    await chat.start()
    try:
        proxy = chat.proxy()

        class StreamCollector(EventHandler):
            def __init__(self) -> None:
                super().__init__(subscriptions=["session.stream"], handle_timeout=1.0)
                self.kinds: list[str] = []

            async def handle(
                self, payload: Any, bus_proxy: EventBus.Proxy, raw_event: Any
            ) -> None:
                del bus_proxy, raw_event
                if isinstance(payload, SessionStreamPayload):
                    self.kinds.append(payload.kind)

        collector = StreamCollector()
        collector_runtime = make_runtime(hub, "stream.collector")
        await collector_runtime.start()
        await collector_runtime.register(collector)

        first = asyncio.create_task(
            core.handle_prompt(
                SessionPromptRequest(session_id="q2", request_id="a", text="hi"), proxy
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=5.0)
        second = asyncio.create_task(
            core.handle_prompt(
                SessionPromptRequest(session_id="q2", request_id="b", text="again"),
                proxy,
            )
        )
        await wait_until(lambda: "queued" in collector.kinds)
        client.release.set()
        assert (await first).success is True
        assert (await second).success is True
        await collector_runtime.stop()
    finally:
        await chat.stop()
        await core.stop()
