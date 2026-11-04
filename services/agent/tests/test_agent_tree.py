"""Agent 侧对话树测试：分支历史、重试即分支、编辑即分支。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from blue_contracts_session import (
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
)
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_registry import ToolRegistryService
from infinity_agent import Message, MessageRole
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import (
    MemoryRuntime,
    MemorySessionGateway,
    ScriptedClient,
    make_runtime,
)

from blue.transport import MemoryFanout

SYSTEM = "sys"
"""测试用 system 提示：让「当前链」的第一条消息内容可预测。"""


@dataclass
class Harness:
    """一套跑起来的 agent：内存会话网关 + 脚本化模型 + 空注册中心。"""

    core: AgentCore
    gateway: MemorySessionGateway
    client: ScriptedClient
    proxy: EventBus.Proxy

    async def ask(self, session_id: str, **payload: object) -> SessionReplyResponse:
        """发一条 prompt 并等最终回复。"""
        response = await request(
            self.proxy,
            SessionPromptEvent.name,
            payload,
            SessionReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        assert isinstance(response, SessionReplyResponse)
        return response


@asynccontextmanager
async def _harness(script: list[Message]) -> AsyncGenerator[Harness]:
    """启动 agent、注册中心与调用方代理。

    注册中心即使没有任何工具也要起：否则每一步都会等目录查询超时。
    """
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    gateway = MemorySessionGateway()
    client = ScriptedClient(script)
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(
            api_key="sk-test", max_steps=4, system_prompt=SYSTEM, compact_history=False
        ),
        client=client,
        session_gateway=gateway,
    )
    chat: MemoryRuntime = make_runtime(hub, "chat")
    await registry.start()
    await core.start()
    await chat.start()
    try:
        yield Harness(core=core, gateway=gateway, client=client, proxy=chat.proxy())
    finally:
        await chat.stop()
        await core.stop()
        await registry.stop()


def test_prompt_hangs_under_the_current_leaf() -> None:
    """连续发言是一条链：每条新消息都挂在上一条下面。"""
    asyncio.run(_prompt_hangs_under_leaf())


async def _prompt_hangs_under_leaf() -> None:
    async with _harness(
        [Message.assistant(content="答一"), Message.assistant(content="答二")]
    ) as run:
        await run.ask("s1", text="问一")
        await run.ask("s1", text="问二")
        tree = run.gateway.tree("s1")
        chain = tree.chain()
        assert [message.content for message in run.gateway.messages["s1"]] == [
            SYSTEM,
            "问一",
            "答一",
            "问二",
            "答二",
        ]
        # 单链：每个节点的父就是链上前一个
        parents = [tree.get(node_id) for node_id in chain]
        assert [node.parent for node in parents if node is not None][1:] == chain[:-1]


def test_retry_opens_a_sibling_reply() -> None:
    """重试 = 在同一个用户消息下再生成一条回复，旧回复留在另一条分支。"""
    asyncio.run(_retry_as_branch())


async def _retry_as_branch() -> None:
    async with _harness(
        [Message.assistant(content="原答案"), Message.assistant(content="重答")]
    ) as run:
        await run.ask("s1", text="问题")
        tree = run.gateway.tree("s1")
        question = tree.chain()[-2]
        first_answer = tree.chain()[-1]

        response = await run.ask("s1", regenerate=True, parent=question)
        assert response.success is True
        assert response.text == "重答"

        after = run.gateway.tree("s1")
        branch = after.chain()[-1]
        assert branch != first_answer
        assert after.children(question) == [first_answer, branch]
        assert after.get(question).current == branch  # type: ignore[union-attr]
        assert [message.content for message in run.gateway.messages["s1"]][-1] == "重答"
        assert len(after.subtree(question)) == 3


def test_prompt_on_an_older_node_gets_only_that_chain() -> None:
    """在旧分支上继续发言：模型只看到那条链的历史，兄弟分支不串味。"""
    asyncio.run(_older_node_history())


async def _older_node_history() -> None:
    async with _harness(
        [Message.assistant(content="答案 A"), Message.assistant(content="答案 B")]
    ) as run:
        await run.ask("s1", text="问题")
        tree = run.gateway.tree("s1")
        question = tree.chain()[-2]
        first_answer = tree.chain()[-1]
        # 同一个问题下多生成一条分支，当前选中的是新的那条
        await run.ask("s1", regenerate=True, parent=question)
        assert run.gateway.tree("s1").chain()[-1] != first_answer

        # 回到第一版回答后面继续追问：历史只包含这条链
        await run.ask("s1", text="追问", parent=first_answer)

        seen = [message.content for message in run.client.calls[-1]]
        assert seen == [SYSTEM, "问题", "答案 A", "追问"]
        assert "答案 B" not in seen


def test_edit_opens_a_branch_instead_of_rewriting() -> None:
    """编辑用户消息 = 在同一个父节点下挂新消息，旧消息与其回答原样保留。"""
    asyncio.run(_edit_as_branch())


async def _edit_as_branch() -> None:
    async with _harness(
        [Message.assistant(content="旧答案"), Message.assistant(content="新答案")]
    ) as run:
        await run.ask("s1", text="原问题")
        tree = run.gateway.tree("s1")
        original = tree.chain()[-2]
        parent = tree.get(original).parent  # type: ignore[union-attr]

        await run.ask("s1", text="改过的问题", parent=parent)

        after = run.gateway.tree("s1")
        current = after.chain()
        assert [message.content for message in run.gateway.messages["s1"]] == [
            SYSTEM,
            "改过的问题",
            "新答案",
        ]
        assert after.children(parent) == [original, current[-2]]
        # 旧问题与其回答仍在树里，只是不在当前链上
        assert after.get(original) is not None
        assert len(after.subtree(original)) == 2


def test_prompt_on_a_missing_node_fails_loudly() -> None:
    """挂载点已过期时立刻报错，不写任何消息、也不调用模型。"""
    asyncio.run(_missing_parent())


async def _missing_parent() -> None:
    async with _harness([Message.assistant(content="不该出现")]) as run:
        response = await run.ask("s1", text="孤儿", parent=999)
        assert response.success is False
        assert "999" in (response.error_msg or "")
        assert run.client.calls == []
        # 只有 ensure 建出来的 system 节点，没有别的消息
        stored = run.gateway.messages["s1"]
        assert [message.role for message in stored] == [MessageRole.SYSTEM]


def test_regenerate_without_history_still_answers() -> None:
    """只有 system 的会话直接重试：没有历史也不报错，回复挂在链尾。"""
    asyncio.run(_regenerate_from_empty())


async def _regenerate_from_empty() -> None:
    async with _harness([Message.assistant(content="凭空回答")]) as run:
        response = await run.ask("s1", regenerate=True)
        assert response.success is True
        assert response.text == "凭空回答"
        tree = run.gateway.tree("s1")
        chain = tree.chain()
        assert [message.content for message in run.gateway.messages["s1"]] == [
            SYSTEM,
            "凭空回答",
        ]
        assert tree.get(chain[-1]).parent == chain[0]  # type: ignore[union-attr]
