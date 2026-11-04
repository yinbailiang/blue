"""端到端：真实 session 服务 + 真实 agent 的发件箱链路。

会话忙时发的第二、第三条消息进真实 SQLite 队列，第一条跑完后由队列泵自动续跑。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from blue_contracts_session import (
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionOutboxListEvent,
    SessionOutboxListReplyEvent,
    SessionOutboxListResponse,
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
)
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_registry import ToolRegistryService
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message, Messages, StreamChunk
from infinity_agent.clients import RequestConfig
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import (
    MemoryRuntime,
    ScriptedClient,
    make_runtime,
    wait_until,
)

from blue.messages import decode_message
from blue.transport import MemoryFanout


class GatedClient(ScriptedClient):
    """第一次调用卡在闸门上，用来制造「会话忙」的窗口。"""

    def __init__(self) -> None:
        super().__init__([Message.assistant(content=f"答{i}") for i in range(10)])
        self.gate = asyncio.Event()
        self.attempts = 0

    async def stream_chat(
        self, messages: Messages, config: RequestConfig | None = None
    ) -> AsyncGenerator[StreamChunk]:
        self.attempts += 1
        if self.attempts == 1:
            await self.gate.wait()
        async for chunk in super().stream_chat(messages, config):
            yield chunk
        if self.attempts == 1:
            self.gate.set()  # 只卡第一次


class Stack:
    """真实 session 服务 + 真实 agent，传输换成进程内 fanout。"""

    def __init__(self, database: Path) -> None:
        self.hub = MemoryFanout()
        self.session = SessionService(
            config=SessionConfig(database_path=str(database)),
            runtime=make_runtime(self.hub, "session"),
        )
        self.registry = ToolRegistryService(
            runtime=make_runtime(self.hub, "registry"), ttl=5.0, sweep_interval=0.05
        )
        self.client = GatedClient()
        self.agent = AgentCore(
            runtime=make_runtime(self.hub, "agent"),
            settings=AgentConfig(
                api_key="sk-test",
                max_steps=4,
                system_prompt="sys",
                outbox_max_attempts=2,
            ),
            client=self.client,
        )
        self.caller: MemoryRuntime = make_runtime(self.hub, "caller")

    async def __aenter__(self) -> Stack:
        await self.session.start()
        await self.registry.start()
        await self.agent.start()
        await self.caller.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for stop in (
            self.caller.stop,
            self.agent.stop,
            self.registry.stop,
            self.session.stop,
        ):
            await stop()

    @property
    def proxy(self) -> EventBus.Proxy:
        """调用方代理。"""
        return self.caller.proxy()

    async def say(self, text: str) -> SessionReplyResponse:
        """发一条 prompt 并等最终回复。"""
        reply = await request(
            self.proxy,
            SessionPromptEvent.name,
            {"text": text},
            SessionReplyEvent.name,
            session_id="s1",
            timeout=20.0,
        )
        assert isinstance(reply, SessionReplyResponse)
        return reply

    async def outbox(self) -> list[tuple[str, str]]:
        """当前队列（正文, 状态）。"""
        reply = await request(
            self.proxy,
            SessionOutboxListEvent.name,
            {},
            SessionOutboxListReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(reply, SessionOutboxListResponse)
        return [(item.text, item.state) for item in reply.messages]


def test_busy_session_queues_then_drains(tmp_path: Path) -> None:
    """忙时把消息排进真实队列，跑完后自动续跑，顺序不丢。"""
    asyncio.run(_busy_then_drain(tmp_path))


async def _busy_then_drain(tmp_path: Path) -> None:
    async with Stack(tmp_path / "sessions.db") as stack:
        # 先建会话（session.get 会 ensure）
        await request(
            stack.proxy,
            SessionGetEvent.name,
            {"system_prompt": "sys"},
            SessionGetReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )

        first = asyncio.create_task(stack.say("第一条"))
        # 等第一条真正开始（闸门卡住模型调用）
        await wait_until(lambda: stack.client.attempts >= 1, timeout=10.0)

        # 第二条、第三条插进来：会话忙 → 进队列
        second = await stack.say("第二条")
        assert second.queued is True
        third = await stack.say("第三条")
        assert third.queued is True
        assert [text for text, _ in await stack.outbox()] == ["第二条", "第三条"]

        # 放行第一条：队列泵应该接着跑第二、三条
        stack.client.gate.set()
        await first
        await _wait_queue_empty(stack)

        # 三条都在库里，顺序正确
        reply = await request(
            stack.proxy,
            SessionGetEvent.name,
            {"system_prompt": ""},
            SessionGetReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(reply, SessionGetResponse)
        assert reply.session is not None
        snapshot = reply.session

        contents = [decode_message(node.message).content for node in snapshot.nodes]
        user_texts = [
            text for text in contents if text in {"第一条", "第二条", "第三条"}
        ]
        assert user_texts == ["第一条", "第二条", "第三条"]


async def _wait_queue_empty(stack: Stack) -> None:
    """等到队列彻底空掉。"""
    deadline = asyncio.get_running_loop().time() + 20.0
    while asyncio.get_running_loop().time() < deadline:
        if not await stack.outbox():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("队列没有被清空")
