"""发件箱队列的总线 RPC：入队、重排、编辑、重试、清空、认领与完成。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue_contracts_session import (
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionOutboxClaimEvent,
    SessionOutboxClaimReplyEvent,
    SessionOutboxClaimResponse,
    SessionOutboxClearEvent,
    SessionOutboxClearReplyEvent,
    SessionOutboxClearResponse,
    SessionOutboxCompleteEvent,
    SessionOutboxCompleteReplyEvent,
    SessionOutboxEnqueueEvent,
    SessionOutboxEnqueueReplyEvent,
    SessionOutboxEnqueueResponse,
    SessionOutboxFailEvent,
    SessionOutboxFailReplyEvent,
    SessionOutboxListEvent,
    SessionOutboxListReplyEvent,
    SessionOutboxListResponse,
    SessionOutboxMoveEvent,
    SessionOutboxMoveReplyEvent,
    SessionOutboxMoveResponse,
    SessionOutboxRemoveEvent,
    SessionOutboxRemoveReplyEvent,
    SessionOutboxRemoveResponse,
    SessionOutboxRetryEvent,
    SessionOutboxRetryReplyEvent,
    SessionOutboxRetryResponse,
    SessionOutboxSendNowEvent,
    SessionOutboxSendNowReplyEvent,
    SessionOutboxSendNowResponse,
    SessionOutboxUpdateEvent,
    SessionOutboxUpdateReplyEvent,
    SessionOutboxUpdateResponse,
    SessionRenameEvent,
    SessionRenameReplyEvent,
    SessionRenameResponse,
)
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import make_runtime

from blue.transport import MemoryFanout


async def _stack(tmp_path: Path) -> tuple[SessionService, EventBus.Proxy]:
    """起一个 session 服务 + 调用方代理，并建好一个会话。"""
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(database_path=str(tmp_path / "sessions.db")),
        runtime=make_runtime(hub, "session"),
    )
    client = make_runtime(hub, "client")
    await service.start()
    await client.start()
    await request(
        client.proxy(),
        SessionGetEvent.name,
        {"system_prompt": "sys"},
        SessionGetReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    return service, client.proxy()


async def _enqueue(proxy: EventBus.Proxy, text: str) -> str:
    reply = await request(
        proxy,
        SessionOutboxEnqueueEvent.name,
        {"text": text},
        SessionOutboxEnqueueReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    assert isinstance(reply, SessionOutboxEnqueueResponse)
    assert reply.success is True
    return reply.queued.id if reply.queued else ""


async def _list(proxy: EventBus.Proxy) -> list[tuple[str, str]]:
    reply = await request(
        proxy,
        SessionOutboxListEvent.name,
        {},
        SessionOutboxListReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    assert isinstance(reply, SessionOutboxListResponse)
    return [(item.text, item.state) for item in reply.messages]


def test_outbox_api_roundtrip(tmp_path: Path) -> None:
    """入队 → 重排 → 编辑 → 认领 → 完成 / 失败重试 → 清空，全走 RPC。"""
    asyncio.run(_outbox_roundtrip(tmp_path))


async def _outbox_roundtrip(tmp_path: Path) -> None:
    service, proxy = await _stack(tmp_path)
    try:
        first = await _enqueue(proxy, "第一条")
        second = await _enqueue(proxy, "第二条")
        assert await _list(proxy) == [("第一条", "pending"), ("第二条", "pending")]

        # 立刻发送 = 置顶
        moved = await request(
            proxy,
            SessionOutboxSendNowEvent.name,
            {"message_id": second},
            SessionOutboxSendNowReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(moved, SessionOutboxSendNowResponse)
        assert moved.success is True
        assert await _list(proxy) == [("第二条", "pending"), ("第一条", "pending")]

        # 下移一位
        down = await request(
            proxy,
            SessionOutboxMoveEvent.name,
            {"message_id": second, "to": "down"},
            SessionOutboxMoveReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(down, SessionOutboxMoveResponse) and down.success
        assert await _list(proxy) == [("第一条", "pending"), ("第二条", "pending")]

        # 编辑正文
        updated = await request(
            proxy,
            SessionOutboxUpdateEvent.name,
            {"message_id": first, "text": "改过的第一条"},
            SessionOutboxUpdateReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(updated, SessionOutboxUpdateResponse) and updated.success
        assert await _list(proxy) == [
            ("改过的第一条", "pending"),
            ("第二条", "pending"),
        ]

        # 认领 → 状态变 sending，行仍在
        claimed = await request(
            proxy,
            SessionOutboxClaimEvent.name,
            {},
            SessionOutboxClaimReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(claimed, SessionOutboxClaimResponse)
        assert claimed.message is not None
        assert claimed.message.text == "改过的第一条"
        assert claimed.message.state == "sending"
        assert await _list(proxy) == [
            ("改过的第一条", "sending"),
            ("第二条", "pending"),
        ]

        # 发送中不能改
        blocked = await request(
            proxy,
            SessionOutboxUpdateEvent.name,
            {"message_id": claimed.message.id, "text": "不允许"},
            SessionOutboxUpdateReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(blocked, SessionOutboxUpdateResponse)
        assert blocked.success is False

        # 失败 → 留原因；重试 → 回到待发
        failed = await request(
            proxy,
            SessionOutboxFailEvent.name,
            {"message_id": claimed.message.id, "error": "模型超时"},
            SessionOutboxFailReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert failed.success is True
        listed = await request(
            proxy,
            SessionOutboxListEvent.name,
            {},
            SessionOutboxListReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(listed, SessionOutboxListResponse)
        head = listed.messages[0]
        assert (head.state, head.error, head.attempts) == ("failed", "模型超时", 1)

        retried = await request(
            proxy,
            SessionOutboxRetryEvent.name,
            {"message_id": head.id},
            SessionOutboxRetryReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(retried, SessionOutboxRetryResponse) and retried.success
        assert await _list(proxy) == [
            ("改过的第一条", "pending"),
            ("第二条", "pending"),
        ]

        # 完成一条 → 从队列删除
        claimed_again = await request(
            proxy,
            SessionOutboxClaimEvent.name,
            {},
            SessionOutboxClaimReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(claimed_again, SessionOutboxClaimResponse)
        assert claimed_again.message is not None
        done = await request(
            proxy,
            SessionOutboxCompleteEvent.name,
            {"message_id": claimed_again.message.id},
            SessionOutboxCompleteReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert done.success is True
        assert await _list(proxy) == [("第二条", "pending")]

        # 单条取消
        removed = await request(
            proxy,
            SessionOutboxRemoveEvent.name,
            {"message_id": second},
            SessionOutboxRemoveReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(removed, SessionOutboxRemoveResponse) and removed.success
        assert await _list(proxy) == []

        # 清空：发送中的保留
        keep = await _enqueue(proxy, "保留")
        drop = await _enqueue(proxy, "被清掉")
        assert drop
        await request(
            proxy,
            SessionOutboxClaimEvent.name,
            {},
            SessionOutboxClaimReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        cleared = await request(
            proxy,
            SessionOutboxClearEvent.name,
            {},
            SessionOutboxClearReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(cleared, SessionOutboxClearResponse)
        assert cleared.removed == 1
        assert await _list(proxy) == [("保留", "sending")]
        assert keep
    finally:
        await service.stop()


def test_rename_rpc_updates_title(tmp_path: Path) -> None:
    """改名 RPC 真的被订阅并回报落库后的标题（P1 的接线漏过一次）。"""
    asyncio.run(_rename_rpc(tmp_path))


async def _rename_rpc(tmp_path: Path) -> None:
    service, proxy = await _stack(tmp_path)
    try:
        await _enqueue(proxy, "第一条消息")
        renamed = await request(
            proxy,
            SessionRenameEvent.name,
            {"title": "手改的名字"},
            SessionRenameReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(renamed, SessionRenameResponse)
        assert renamed.success is True
        assert renamed.title == "手改的名字"
    finally:
        await service.stop()
