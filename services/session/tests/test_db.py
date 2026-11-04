"""会话数据库测试：WAL 持久化、消息追加、回收与容量上限。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue_service_session.db import SessionDatabase, SessionNotFoundError
from infinity_agent import Message, MessageRole

from blue.messages import decode_messages, encode_message


def test_ensure_creates_system_message(tmp_path: Path) -> None:
    """首次取会话会写入 system 消息。"""
    asyncio.run(_ensure_creates_system_message(tmp_path))


async def _ensure_creates_system_message(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        record = await db.ensure("s1", "系统提示", now=100.0)
        assert record.meta.session_id == "s1"
        assert record.meta.message_count == 1
        assert record.meta.created_at == 100.0
        messages = decode_messages(record.messages)
        assert [message.role for message in messages] == [MessageRole.SYSTEM]
        assert messages[0].content == "系统提示"
    finally:
        await db.close()


def test_append_update_and_reopen(tmp_path: Path) -> None:
    """追加的消息与步数在重开连接后仍然存在。"""
    asyncio.run(_append_update_and_reopen(tmp_path))


async def _append_update_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    db = SessionDatabase(path)
    await db.connect()
    try:
        await db.ensure("s1", "sys", now=100.0)
        meta = await db.append(
            "s1",
            [encode_message(Message.user("hi"))],
            now=101.0,
        )
        assert meta.message_count == 2
        updated = await db.update("s1", steps=3, touch=True, now=102.0)
        assert updated.steps == 3
    finally:
        await db.close()

    reopened = SessionDatabase(path)
    await reopened.connect()
    try:
        record = await reopened.get("s1")
        assert record is not None
        assert record.meta.steps == 3
        assert [message.role for message in decode_messages(record.messages)] == [
            MessageRole.SYSTEM,
            MessageRole.USER,
        ]
    finally:
        await reopened.close()


def test_reset_removes_messages(tmp_path: Path) -> None:
    """重置删除会话，再次读取报不存在。"""
    asyncio.run(_reset_removes_messages(tmp_path))


async def _reset_removes_messages(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "sys", now=100.0)
        assert await db.reset("s1") is True
        assert await db.get("s1") is None
        try:
            await db.meta("s1")
        except SessionNotFoundError:
            pass
        else:
            raise AssertionError("会话应已删除")
    finally:
        await db.close()


def test_prune_idle_and_capacity(tmp_path: Path) -> None:
    """空闲会话与超限会话都会被回收。"""
    asyncio.run(_prune_idle_and_capacity(tmp_path))


async def _prune_idle_and_capacity(tmp_path: Path) -> None:
    db = SessionDatabase(
        tmp_path / "sessions.db",
        max_sessions=2,
        idle_ttl=10.0,
        enforce_capacity=True,
    )
    await db.connect()
    try:
        await db.ensure("old", "sys", now=100.0)
        await db.ensure("new", "sys", now=105.0)
        result = await db.prune(now=200.0)
        assert result.removed == ["old", "new"]
        assert len(result.expired) == 2
        assert result.evicted == []
        assert await db.state() == []

        await db.ensure("s1", "sys", now=200.0)
        await db.ensure("s2", "sys", now=201.0)
        result = await db.prune(now=201.0)
        assert result.removed == []
        await db.ensure("s3", "sys", now=202.0)
        state = await db.state()
        assert [item.session_id for item in state] == ["s3", "s2"]
    finally:
        await db.close()


def test_prune_protects_recent_sessions_from_capacity_eviction(
    tmp_path: Path,
) -> None:
    """active_grace 内的会话不参与容量淘汰，宁可变相关闭也不误删。"""
    asyncio.run(_prune_protects_recent(tmp_path))


async def _prune_protects_recent(tmp_path: Path) -> None:
    db = SessionDatabase(
        tmp_path / "sessions.db",
        max_sessions=1,
        idle_ttl=10_000.0,
        active_grace=120.0,
        enforce_capacity=True,
    )
    await db.connect()
    try:
        await db.ensure("busy", "sys", now=100.0)
        await db.ensure("newer", "sys", now=110.0)
        result = await db.prune(now=200.0)
        # 两个都在 120s 保护期内：一个都不淘汰
        assert result.removed == []
        assert [item.session_id for item in await db.state()] == ["newer", "busy"]

        # 过了保护期后，最久未活动的 busy 先被淘汰
        result = await db.prune(now=400.0)
        assert result.evicted == ["busy"]
        assert [item.session_id for item in await db.state()] == ["newer"]
    finally:
        await db.close()


def test_prune_batch_size_limits_one_pass(tmp_path: Path) -> None:
    """单次回收不超过 prune_batch_size 个，剩下的下一轮再处理。"""
    asyncio.run(_prune_batch_limits(tmp_path))


async def _prune_batch_limits(tmp_path: Path) -> None:
    db = SessionDatabase(
        tmp_path / "sessions.db",
        max_sessions=100,
        idle_ttl=10.0,
        prune_batch_size=2,
        enforce_capacity=True,
    )
    await db.connect()
    try:
        for index in range(5):
            await db.ensure(f"s{index}", "sys", now=100.0)
        first = await db.prune(now=1000.0)
        assert first.total == 2
        assert len(await db.state()) == 3
        second = await db.prune(now=1000.0)
        assert second.total == 2
        third = await db.prune(now=1000.0)
        assert third.total == 1
        assert await db.state() == []
    finally:
        await db.close()


def test_prune_disabled_keeps_everything(tmp_path: Path) -> None:
    """自动回收关闭时，即使超上限或很旧也不删（只靠人工 reset）。"""
    asyncio.run(_prune_disabled(tmp_path))


async def _prune_disabled(tmp_path: Path) -> None:
    db = SessionDatabase(
        tmp_path / "sessions.db",
        max_sessions=1,
        idle_ttl=10.0,
        enforce_capacity=False,
    )
    await db.connect()
    try:
        await db.ensure("a", "sys", now=100.0)
        await db.ensure("b", "sys", now=101.0)
        await db.ensure("c", "sys", now=102.0)
        assert [item.session_id for item in await db.state()] == ["c", "b", "a"]
        # service 层在关闭时不会调用 prune；这里即使调用也只会按 TTL 走，
        # 而容量淘汰必须被禁用。
        result = await db.prune(now=10_000.0)
        assert result.evicted == []
    finally:
        await db.close()
