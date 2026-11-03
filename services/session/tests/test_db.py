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
    db = SessionDatabase(tmp_path / "sessions.db", max_sessions=2, idle_ttl=10.0)
    await db.connect()
    try:
        await db.ensure("old", "sys", now=100.0)
        await db.ensure("new", "sys", now=105.0)
        removed = await db.prune(now=200.0)
        assert removed == ["old", "new"]
        assert await db.state() == []

        await db.ensure("s1", "sys", now=200.0)
        await db.ensure("s2", "sys", now=201.0)
        removed = await db.prune(now=201.0)
        assert removed == []
        await db.ensure("s3", "sys", now=202.0)
        state = await db.state()
        assert [item.session_id for item in state] == ["s3", "s2"]
    finally:
        await db.close()
