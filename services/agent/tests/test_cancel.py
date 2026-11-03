"""中断测试：取消进行中的回复，并确保中断后不留下幽灵消息或悬空 tool_calls。"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncGenerator

from blue_contracts_session import (
    SessionPromptRequest,
    SessionSnapshot,
)
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_registry import ToolRegistryService
from infinity_agent import Message, MessageRole, Messages
from infinity_agent.clients import (
    FinishChunk,
    LLMClient,
    RequestConfig,
    StreamChunk,
    TextChunk,
    ThinkingChunk,
)
from infinity_bus import EventBus
from tests.support import make_runtime, wait_until

from blue.messages import encode_messages
from blue.transport import MemoryFanout


class RecordingGateway:
    """只记录消息的内存会话网关。"""

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
        self, bus_proxy: EventBus.Proxy, session_id: str, messages: list[Message]
    ) -> None:
        del bus_proxy
        self.messages.setdefault(session_id, []).extend(messages)

    async def update(
        self, bus_proxy: EventBus.Proxy, session_id: str, steps: int
    ) -> None:
        del bus_proxy
        self.steps[session_id] = steps


class HangingClient(LLMClient):
    """先吐出固定增量，然后挂住，供测试在流式阶段发起中断。"""

    def __init__(self, *, text: str = "", thinking: str = "") -> None:
        self._text = text
        self._thinking = thinking
        self.calls: list[Messages] = []

    async def __aenter__(self) -> HangingClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> bool | None:
        return None

    async def stream_chat(
        self,
        messages: Messages,
        config: RequestConfig | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        del config
        self.calls.append(list(messages))
        if self._thinking:
            yield ThinkingChunk(text=self._thinking)
        if self._text:
            yield TextChunk(text=self._text)
        await asyncio.sleep(3600)
        yield FinishChunk(finish_reason="stop")


async def _start_core(
    hub: MemoryFanout,
    client: LLMClient,
    gateway: RecordingGateway,
) -> tuple[AgentCore, ToolRegistryService, EventBus.Proxy]:
    """启动一个最小的 agent 运行环境，返回核心、注册中心与调用方代理。"""
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4),
        client=client,
        session_gateway=gateway,
    )
    chat = make_runtime(hub, "chat")
    await registry.start()
    await core.start()
    await chat.start()
    return core, registry, chat.proxy()


def test_cancel_without_active_turn_returns_false() -> None:
    """没有进行中的回复时，取消请求返回 False。"""

    async def scenario() -> None:
        gateway = RecordingGateway()
        core = AgentCore(
            runtime=make_runtime(MemoryFanout(), "agent"),
            settings=AgentConfig(api_key="sk-test"),
            client=HangingClient(),
            session_gateway=gateway,
        )
        assert core.cancel("nothing-running") is False

    asyncio.run(scenario())


def test_cancel_mid_stream_keeps_partial_answer_marked() -> None:
    """生成中途被中断：已产生的文本落库并标记「已停止」。"""

    async def scenario() -> None:
        hub = MemoryFanout()
        gateway = RecordingGateway()
        client = HangingClient(text="半截回答")
        core, registry, proxy = await _start_core(hub, client, gateway)
        try:
            task = asyncio.create_task(
                core.handle_prompt(
                    SessionPromptRequest(
                        session_id="s-cancel",
                        request_id="r1",
                        text="讲个故事",
                    ),
                    proxy,
                )
            )
            await wait_until(lambda: len(gateway.messages.get("s-cancel", [])) >= 2)
            await asyncio.sleep(0.05)
            assert core.cancel("s-cancel") is True
            response = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await core.stop()
            await registry.stop()

        assert response.success is False
        assert response.error_msg == "已中断"
        stored = gateway.messages["s-cancel"]
        assert [message.role for message in stored] == [
            MessageRole.SYSTEM,
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        assistant = stored[-1]
        assert isinstance(assistant.content, str)
        assert "半截回答" in assistant.content
        assert "已停止" in assistant.content

    asyncio.run(scenario())


def test_cancel_mid_stream_without_output_leaves_no_message() -> None:
    """思考阶段就被中断且一个字没流出：库里不留任何消息（消灭幽灵消息）。"""

    async def scenario() -> None:
        hub = MemoryFanout()
        gateway = RecordingGateway()
        client = HangingClient()
        core, registry, proxy = await _start_core(hub, client, gateway)
        try:
            task = asyncio.create_task(
                core.handle_prompt(
                    SessionPromptRequest(
                        session_id="s-silent",
                        request_id="r2",
                        text="讲个故事",
                    ),
                    proxy,
                )
            )
            await wait_until(lambda: len(gateway.messages.get("s-silent", [])) >= 2)
            await asyncio.sleep(0.05)
            assert core.cancel("s-silent") is True
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await core.stop()
            await registry.stop()

        assert [message.role for message in gateway.messages["s-silent"]] == [
            MessageRole.SYSTEM,
            MessageRole.USER,
        ]

    asyncio.run(scenario())
