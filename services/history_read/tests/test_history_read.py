"""history_read 工具：会话上下文要求 + 按 id 读回原文的端到端。"""

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
)
from blue_service_history_read import history_read
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message, MessageRole
from infinity_bus.templates.request import request
from tests.support import MemoryRuntime, make_runtime

from blue.messages import encode_messages
from blue.tool_context import ToolContext, bind_runtime_scoped, use_context
from blue.transport import MemoryFanout

LONG_BODY = "x" * 5000
"""一条超长消息：用来验证截断标记。"""


@dataclass
class Harness:
    """跑起来的 session 服务 + 调用方。"""

    runtime: MemoryRuntime


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncGenerator[Harness]:
    """起 session 服务，写入一条长消息。"""
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(database_path=str(tmp_path / "sessions.db")),
        runtime=make_runtime(hub, "session"),
    )
    caller = make_runtime(hub, "history_read")
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
            {"messages": encode_messages([Message.user(LONG_BODY)])},
            SessionAppendReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        yield Harness(runtime=caller)
    finally:
        await caller.stop()
        await service.stop()


def test_requires_session_context() -> None:
    """没有 session_id 就报错；也不会把 ids 当空数组糊过去。"""
    with pytest.raises(RuntimeError, match="session_id"):
        asyncio.run(history_read([1]))


def test_rejects_empty_ids() -> None:
    """ids 为空要报错（模型偶尔会传空数组）。"""

    async def _call() -> None:
        with use_context(ToolContext(session_id="s1")):
            await history_read([])

    with pytest.raises(RuntimeError, match="ids"):
        asyncio.run(_call())


async def _read(
    tmp_path: Path, node_id: int, *, max_chars: int
) -> tuple[int, bool, str, list[int]]:
    """在工具上下文里读一条。"""
    async with _harness(tmp_path) as run:
        with (
            bind_runtime_scoped(run.runtime),
            use_context(
                ToolContext(session_id="s1", request_id="req-1", tool="history_read")
            ),
        ):
            result = await history_read([node_id], max_chars=max_chars)
        message = result.messages[0]
        return message.chars, message.truncated, message.content, result.missing


def test_reads_full_text(tmp_path: Path) -> None:
    """读原文：给全量文本，不截断。"""
    chars, truncated, content, missing = asyncio.run(
        _read(tmp_path, 2, max_chars=100_000)
    )
    assert chars == len(LONG_BODY)
    assert truncated is False
    assert content == LONG_BODY
    assert missing == []


def test_truncates_and_reports_real_length(tmp_path: Path) -> None:
    """超上限时截断，并把真实长度写进正文尾部，模型才知道还有多少。"""
    chars, truncated, content, _ = asyncio.run(_read(tmp_path, 2, max_chars=300))
    assert truncated is True
    assert chars == len(LONG_BODY)
    assert content.endswith(f"[{chars} chars total]")


def test_tool_context_is_what_selects_the_session(tmp_path: Path) -> None:
    """同样是读 id=2，换个会话上下文就查不到——说明 session 来自上下文而非参数。"""

    async def _other_session(tmp_path: Path) -> list[int]:
        async with _harness(tmp_path) as run:
            with (
                bind_runtime_scoped(run.runtime),
                use_context(
                    ToolContext(
                        session_id="missing", request_id="r", tool="history_read"
                    )
                ),
            ):
                result = await history_read([2])
            return result.missing

    missing = asyncio.run(_other_session(tmp_path))
    assert missing == [2]


assert MessageRole  # 保留导入，说明工具结果是按角色标注的
