"""history_search 工具：会话上下文要求 + 经 ``session.search`` 取数的端到端。

工具本身不碰数据库——它只把工具调用上下文里的 session_id 转成一次 RPC，
历史归会话服务所有。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from blue_contracts_session import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionSearchResponse,
)
from blue_service_history_search import history_search
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message
from infinity_agent.tools import ToolRegistry
from infinity_bus.templates.request import request
from tests.support import MemoryRuntime, make_runtime

from blue.messages import encode_messages
from blue.tool_context import ToolContext, bind_runtime_scoped, use_context
from blue.transport import MemoryFanout


@dataclass
class Harness:
    """一个跑起来的 session 服务 + 一个可发 RPC 的调用方。"""

    runtime: MemoryRuntime


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncGenerator[Harness]:
    """起真实的 session 服务（SQLite 落到 tmp），并建好一个会话。"""
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(database_path=str(tmp_path / "sessions.db")),
        runtime=make_runtime(hub, "session"),
    )
    caller = make_runtime(hub, "history_search")
    await service.start()
    await caller.start()
    try:
        proxy = caller.proxy()
        await request(
            proxy,
            SessionGetEvent.name,
            {"system_prompt": "你是测试"},
            SessionGetReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        await request(
            proxy,
            SessionAppendEvent.name,
            {
                "messages": encode_messages(
                    [Message.user("帮我查一下 tree.py 的循环引用")]
                )
            },
            SessionAppendReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        yield Harness(runtime=caller)
    finally:
        await caller.stop()
        await service.stop()


def test_requires_session_context() -> None:
    """没有 session_id（不在会话里调用）就明确报错，而不是悄悄搜空。"""
    with pytest.raises(RuntimeError, match="session_id"):
        asyncio.run(history_search("tree.py"))


async def _search(tmp_path: Path):
    """在工具上下文里真的走一次 RPC。"""
    async with _harness(tmp_path) as run:
        with (
            bind_runtime_scoped(run.runtime),
            use_context(
                ToolContext(session_id="s1", request_id="req-1", tool="history_search")
            ),
        ):
            return await history_search("tree.py")


def test_searches_the_session_history(tmp_path: Path) -> None:
    """端到端：工具 → session.search → 命中带 id、角色、片段。"""
    result = asyncio.run(_search(tmp_path))
    assert result.total == 1
    hit = result.hits[0]
    assert hit.role == "user"
    assert hit.id > 0
    assert "tree.py" in hit.snippet


def test_tool_declaration_matches_the_signature() -> None:
    """单进程单工具，参数 schema 由签名生成（模型能看见三个字段）。"""
    registry = ToolRegistry()
    registry.register(history_search)
    definitions = registry.definitions
    assert len(definitions) == 1
    function = definitions[0].function
    assert function.name == "history_search"
    assert set(function.parameters.properties) == {"query", "limit", "roles"}
    assert function.parameters.required == ["query"]


assert SessionSearchResponse  # 导入用于类型说明，避免 lint 误删
