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
        messages = decode_messages(got.session.messages)
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
