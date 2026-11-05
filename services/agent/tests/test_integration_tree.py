"""端到端：真实 session 服务 + 真实 agent，经总线走完对话树全流程。

与 ``test_agent_tree`` 不同，这里不替换会话网关：prompt / append / 分支切换 /
分支删除都是真实的 ``session.*`` RPC，落到真实的 SQLite 会话库（临时目录）。
只把传输层换成进程内 fanout，避免依赖 RabbitMQ。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue_contracts_session import (
    SessionBranchActivateEvent,
    SessionBranchActivateReplyEvent,
    SessionBranchActivateResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionNode,
    SessionNodeDeleteEvent,
    SessionNodeDeleteReplyEvent,
    SessionNodeDeleteResponse,
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionSnapshot,
)
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_echo import echo
from blue_service_registry import ToolRegistryService
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import ScriptedClient, make_runtime, wait_until

from blue.transport import MemoryFanout
from blue.tree import Tree
from blue_tool_sdk import ToolWorker


class Stack:
    """一套跑起来的真实服务栈：session 服务 + agent + 注册中心 + echo 工具。"""

    def __init__(self, database: Path, script: list[Message]) -> None:
        self.hub = MemoryFanout()
        self.session = SessionService(
            config=SessionConfig(database_path=str(database), prune_enabled=False),
            runtime=make_runtime(self.hub, "session"),
        )
        self.registry = ToolRegistryService(
            runtime=make_runtime(self.hub, "registry"), ttl=5.0, sweep_interval=0.05
        )
        self.worker = ToolWorker(
            runtime=make_runtime(self.hub, "tool.echo"), tool=echo, ttl=5.0
        )
        self.client = ScriptedClient(script)
        self.agent = AgentCore(
            runtime=make_runtime(self.hub, "agent"),
            settings=AgentConfig(api_key="sk-test", max_steps=4, system_prompt="sys"),
            client=self.client,
            owns_runtime=False,
        )
        self.caller = make_runtime(self.hub, "caller")

    async def __aenter__(self) -> Stack:
        await self.session.start()
        await self.registry.start()
        await self.worker.start()
        await self.agent.start()
        await self.caller.start()
        await wait_until(lambda: self.registry.state.worker_count == 1)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for stop in (
            self.caller.stop,
            self.agent.stop,
            self.worker.stop,
            self.registry.stop,
            self.session.stop,
        ):
            await stop()

    @property
    def proxy(self) -> EventBus.Proxy:
        """调用方代理。"""
        return self.caller.proxy()

    async def say(self, text: str, **options: object) -> SessionReplyResponse:
        """发一轮 prompt 并等最终回复（options 可带 parent / regenerate）。"""
        reply = await request(
            self.proxy,
            SessionPromptEvent.name,
            {"text": text, **options},
            SessionReplyEvent.name,
            session_id="s1",
            timeout=15.0,
        )
        assert isinstance(reply, SessionReplyResponse)
        return reply

    async def snapshot(self) -> SessionSnapshot:
        """取会话快照。"""
        response = await request(
            self.proxy,
            SessionGetEvent.name,
            {"system_prompt": ""},
            SessionGetReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(response, SessionGetResponse)
        assert response.session is not None
        return response.session

    async def activate(self, node: int) -> None:
        """切换分支。"""
        response = await request(
            self.proxy,
            SessionBranchActivateEvent.name,
            {"node": node},
            SessionBranchActivateReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(response, SessionBranchActivateResponse)
        assert response.success is True, response.error_msg

    async def drop(self, node: int) -> int:
        """删除分支。"""
        response = await request(
            self.proxy,
            SessionNodeDeleteEvent.name,
            {"node": node},
            SessionNodeDeleteReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(response, SessionNodeDeleteResponse)
        assert response.success is True, response.error_msg
        return response.removed


def test_tree_flow_over_the_real_session_service(tmp_path: Path) -> None:
    """发言 → 重试 → 切换分支 → 删分支，全程走真实 RPC 与 SQLite。"""
    asyncio.run(_tree_flow(tmp_path))


async def _tree_flow(tmp_path: Path) -> None:
    async with Stack(
        tmp_path / "sessions.db",
        [
            Message.assistant(content="第一版回答"),
            Message.assistant(content="第二版回答"),
            Message.assistant(content="改过之后的新回答"),
        ],
    ) as stack:
        # 1. 普通发言：system → user → assistant 一条链
        first = await stack.say("原始问题")
        assert first.text == "第一版回答"
        snapshot = await stack.snapshot()
        tree: Tree[SessionNode] = Tree(
            snapshot.nodes, root_current=snapshot.root_current
        )
        chain = tree.chain()
        assert len(chain) == 3
        assert snapshot.current == chain
        question = chain[1]
        first_answer = chain[2]

        # 2. 重试某条回复：同一父节点下多一条分支，两条回答都在
        retry = await stack.say("", regenerate=True, parent=question)
        assert retry.text == "第二版回答"
        snapshot = await stack.snapshot()
        tree: Tree[SessionNode] = Tree(
            snapshot.nodes, root_current=snapshot.root_current
        )
        second_answer = tree.chain()[-1]
        assert second_answer != first_answer
        assert tree.children(question) == [first_answer, second_answer]
        assert tree.get(question) is not None
        assert tree.get(question).current == second_answer  # type: ignore[union-attr]
        assert len(snapshot.nodes) == 4

        # 3. 切回第一版：当前链换一条，节点一条不少
        await stack.activate(first_answer)
        switched = await stack.snapshot()
        assert switched.current[-1] == first_answer
        assert len(switched.nodes) == 4
        assert [node.id for node in switched.nodes] == [
            node.id for node in snapshot.nodes
        ]

        # 4. 在旧分支上编辑出第三条分支：历史只带这条链
        await stack.say("改过的问题", parent=snapshot.nodes[0].id)
        edited = await stack.snapshot()
        edited_tree: Tree[SessionNode] = Tree(
            edited.nodes, root_current=edited.root_current
        )
        edited_chain = edited_tree.chain()
        assert edited_chain[-1] != first_answer
        seen = [message.content for message in stack.client.calls[-1]]
        assert seen == ["sys", "改过的问题"]
        # 原来的两条回答都还在树里
        assert edited_tree.get(first_answer) is not None
        assert edited_tree.get(second_answer) is not None

        # 5. 删掉一条分支：子树消失，父节点回落到剩下的最新分支
        removed = await stack.drop(second_answer)
        assert removed == 1
        final = await stack.snapshot()
        final_tree: Tree[SessionNode] = Tree(
            final.nodes, root_current=final.root_current
        )
        assert final_tree.get(second_answer) is None
        assert final_tree.children(question) == [first_answer]

        # 6. 删掉原始那条链：编辑出来的分支不受影响，成为唯一历史
        removed = await stack.drop(question)
        assert removed == len(final_tree.subtree(question)) == 2
        left = await stack.snapshot()
        left_tree: Tree[SessionNode] = Tree(left.nodes, root_current=left.root_current)
        assert left_tree.chain() == edited_chain
        assert [node.id for node in left.nodes] == edited_chain
