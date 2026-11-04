"""session 服务的总线 RPC 测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue_contracts_session import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetResponse,
    SessionSnapshot,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateResponse,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateResponse,
)
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message, MessageRole
from infinity_bus.templates.request import request
from tests.support import make_runtime

from blue.messages import decode_messages, encode_message
from blue.transport import MemoryFanout
from blue.tree import Tree


def _chain(snapshot: SessionSnapshot) -> list[Message]:
    """快照里当前链上的消息。"""
    tree = Tree(snapshot.nodes, root_current=snapshot.root_current)
    return decode_messages(node.message for node in tree.collect(tree.chain()))


def test_session_rpc_roundtrip(tmp_path: Path) -> None:
    """get → append → update → state → reset 全链路。"""
    asyncio.run(_session_rpc_roundtrip(tmp_path))


async def _session_rpc_roundtrip(tmp_path: Path) -> None:
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(
            database_path=str(tmp_path / "sessions.db"),
            prune_interval=60.0,
        ),
        runtime=make_runtime(hub, "session"),
    )
    client = make_runtime(hub, "client")
    await service.start()
    await client.start()
    try:
        got = await request(
            client.proxy(),
            SessionGetEvent.name,
            {"system_prompt": "你是测试助手"},
            SessionGetReplyEvent.name,
            session_id="s1",
            timeout=5.0,
        )
        assert isinstance(got, SessionGetResponse)
        assert got.success is True
        assert got.session is not None
        messages = _chain(got.session)
        assert [message.role for message in messages] == [MessageRole.SYSTEM]

        appended = await request(
            client.proxy(),
            SessionAppendEvent.name,
            {"messages": [encode_message(Message.user("你好"))]},
            SessionAppendReplyEvent.name,
            session_id="s1",
            timeout=5.0,
        )
        assert isinstance(appended, SessionAppendResponse)
        assert appended.success is True
        assert appended.message_count == 2

        updated = await request(
            client.proxy(),
            SessionUpdateEvent.name,
            {"steps": 2, "touch": True},
            SessionUpdateReplyEvent.name,
            session_id="s1",
            timeout=5.0,
        )
        assert isinstance(updated, SessionUpdateResponse)
        assert updated.steps == 2
        assert updated.message_count == 2

        state = await request(
            client.proxy(),
            SessionStateEvent.name,
            {},
            SessionStateReplyEvent.name,
            session_id="client",
            timeout=5.0,
        )
        assert isinstance(state, SessionStateResponse)
        assert [item.session_id for item in state.sessions] == ["s1"]

        reset = await request(
            client.proxy(),
            SessionResetEvent.name,
            {},
            SessionResetReplyEvent.name,
            session_id="s1",
            timeout=5.0,
        )
        assert isinstance(reset, SessionResetResponse)
        assert reset.success is True
    finally:
        await client.stop()
        await service.stop()


def test_default_config_keeps_sessions(tmp_path: Path) -> None:
    """默认配置下不自动删：最小惊讶原则——没显式打开就不丢历史。"""
    asyncio.run(_default_config_keeps(tmp_path))


async def _default_config_keeps(tmp_path: Path) -> None:
    config = SessionConfig(
        database_path=str(tmp_path / "sessions.db"),
        max_sessions=1,
        idle_ttl=10.0,
    )
    assert config.prune_enabled is False

    service = SessionService(config=config, runtime=make_runtime(MemoryFanout(), "s"))
    await service.start()
    try:
        # 起点很早、数量超过 max_sessions：默认配置下都不会被删
        for index in range(3):
            await service.database.ensure(f"s{index}", "sys", now=1.0 + index)
        await service.reap_once()
        assert len(await service.database.state()) == 3
    finally:
        await service.stop()


def test_reap_reclaims_stale_sessions_when_enabled(tmp_path: Path) -> None:
    """开关打开时，一轮回收会删掉过期会话。"""
    asyncio.run(_reap_when_enabled(tmp_path))


async def _reap_when_enabled(tmp_path: Path) -> None:
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(
            database_path=str(tmp_path / "sessions.db"),
            idle_ttl=10.0,
            active_grace=0.0,
            prune_enabled=True,
            prune_interval=3600.0,
        ),
        runtime=make_runtime(hub, "session"),
    )
    await service.start()
    try:
        await service.database.ensure("stale", "sys", now=1.0)
        await service.reap_once()
        assert await service.database.state() == []
    finally:
        await service.stop()


def test_reap_keeps_sessions_when_prune_disabled(tmp_path: Path) -> None:
    """prune_enabled=false 时，再旧、再多的会话也不自动删。"""
    asyncio.run(_reap_when_disabled(tmp_path))


async def _reap_when_disabled(tmp_path: Path) -> None:
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(
            database_path=str(tmp_path / "sessions.db"),
            idle_ttl=10.0,
            max_sessions=1,
            prune_enabled=False,
            prune_interval=3600.0,
        ),
        runtime=make_runtime(hub, "session"),
    )
    await service.start()
    try:
        await service.database.ensure("stale", "sys", now=1.0)
        await service.database.ensure("also-stale", "sys", now=2.0)
        await service.reap_once()
        assert [item.session_id for item in await service.database.state()] == [
            "also-stale",
            "stale",
        ]
    finally:
        await service.stop()
