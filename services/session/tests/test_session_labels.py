"""会话标题与摘要：自动生成、改名、恢复自动、排队计数与老库回填。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from blue_service_session.db import SessionDatabase
from infinity_agent import Message

from blue.messages import encode_message


async def _open(tmp_path: Path) -> SessionDatabase:
    database = SessionDatabase(tmp_path / "sessions.db")
    await database.connect()
    return database


def test_title_follows_the_first_user_message(tmp_path: Path) -> None:
    """标题取首条用户消息（system 不算），摘要取最后一条用户消息。"""
    asyncio.run(_title_follows_user_message(tmp_path))


async def _title_follows_user_message(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        await database.ensure("s1", "你是 blue", now=100.0)
        await database.append(
            "s1", [encode_message(Message.user("帮我看下项目结构"))], now=101.0
        )
        record = await database.get("s1")
        assert record is not None
        assert record.meta.title == "帮我看下项目结构"

        await database.append(
            "s1", [encode_message(Message.user("顺便跑一下测试"))], now=102.0
        )
        record = await database.get("s1")
        assert record is not None
        # 标题只认第一条，摘要跟着最新一条
        assert record.meta.title == "帮我看下项目结构"
        assert record.meta.preview == "顺便跑一下测试"
    finally:
        await database.close()


def test_long_message_is_truncated_with_ellipsis(tmp_path: Path) -> None:
    """超长标题截断到 28 字并加省略号。"""
    asyncio.run(_long_message_truncated(tmp_path))


async def _long_message_truncated(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        await database.ensure("s1", "", now=100.0)
        text = "一" * 200
        await database.append("s1", [encode_message(Message.user(text))], now=101.0)
        record = await database.get("s1")
        assert record is not None
        assert record.meta.title == "一" * 28 + "…"
        assert record.meta.preview == "一" * 120 + "…"
    finally:
        await database.close()


def test_rename_and_restore_auto_title(tmp_path: Path) -> None:
    """改名生效；传空字符串则恢复自动标题。"""
    asyncio.run(_rename_and_restore(tmp_path))


async def _rename_and_restore(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        await database.ensure("s1", "", now=100.0)
        await database.append(
            "s1", [encode_message(Message.user("原始问题"))], now=101.0
        )

        assert await database.set_title("s1", "我起的名字") == "我起的名字"
        record = await database.get("s1")
        assert record is not None
        assert record.meta.title == "我起的名字"

        # 空 = 恢复自动：重新从当前链的第一条用户消息推导
        assert await database.set_title("s1", "   ") == "原始问题"
        record = await database.get("s1")
        assert record is not None
        assert record.meta.title == "原始问题"
    finally:
        await database.close()


def test_auto_title_follows_the_current_chain(tmp_path: Path) -> None:
    """换分支后恢复自动标题，取的是当前链上的第一条用户消息。"""
    asyncio.run(_auto_title_follows_chain(tmp_path))


async def _auto_title_follows_chain(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        await database.ensure("s1", "sys", now=100.0)
        record = await database.get("s1")
        assert record is not None
        system_id = record.nodes[0].id
        original = (
            await database.append(
                "s1", [encode_message(Message.user("原问题"))], now=101.0
            )
        ).nodes[0]
        await database.set_title("s1", "手改的名字")

        # 在 system 下开一条兄弟分支（模拟「编辑第一条消息」）
        edited = (
            await database.append(
                "s1",
                [encode_message(Message.user("改过的问题"))],
                now=102.0,
                parent=system_id,
            )
        ).nodes[0]
        assert (await database.get("s1")).meta.title == "手改的名字"  # type: ignore[union-attr]

        # 清空标题 = 按当前链（这条新分支）重新推导
        assert await database.set_title("s1", "") == "改过的问题"
        # 切回旧分支，再清空一次，标题跟着变
        await database.set_current("s1", original)
        assert await database.set_title("s1", "") == "原问题"
        assert edited != original
    finally:
        await database.close()


def test_state_reports_queued_count(tmp_path: Path) -> None:
    """会话列表带出「有几条在排队」，前端侧栏直接用。"""
    asyncio.run(_state_reports_queued(tmp_path))


async def _state_reports_queued(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        await database.ensure("s1", "", now=100.0)
        await database.append("s1", [encode_message(Message.user("你好"))], now=101.0)
        await database.enqueue_message("s1", "排队一", [], now=102.0)
        await database.enqueue_message("s1", "排队二", [], now=103.0)

        infos = await database.state()
        assert [(info.title, info.preview, info.queued) for info in infos] == [
            ("你好", "你好", 2)
        ]

        claimed = await database.claim_message("s1", now=104.0)
        assert claimed is not None
        infos = await database.state()
        assert infos[0].queued == 1
    finally:
        await database.close()


_V3_SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def test_old_database_backfills_titles(tmp_path: Path) -> None:
    """v3 老库升级后：标题/摘要按历史消息补出来，不用用户手动改。"""
    asyncio.run(_old_database_backfills(tmp_path))


async def _old_database_backfills(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(_V3_SCHEMA)
        await connection.execute(
            "INSERT INTO sessions"
            " (session_id, created_at, updated_at, steps, message_count)"
            " VALUES ('s1', 1.0, 1.0, 0, 3)"
        )
        for message in (
            Message.system("sys"),
            Message.user("老会话的第一个问题"),
            Message.assistant(content="回答"),
            Message.user("后来又追问了一次"),
        ):
            await connection.execute(
                "INSERT INTO messages (session_id, payload, created_at)"
                " VALUES (?, ?, ?)",
                ("s1", encode_message(message), 1.0),
            )
        await connection.execute("PRAGMA user_version = 3")
        await connection.commit()
    finally:
        await connection.close()

    database = SessionDatabase(path)
    await database.connect()
    try:
        record = await database.get("s1")
        assert record is not None
        assert record.meta.title == "老会话的第一个问题"
        assert record.meta.preview == "后来又追问了一次"
        cursor = await database.state()
        assert cursor[0].title == "老会话的第一个问题"
    finally:
        await database.close()


def test_rename_unknown_session_fails(tmp_path: Path) -> None:
    """给不存在的会话改名要报错，而不是静默成功。"""
    asyncio.run(_rename_unknown(tmp_path))


async def _rename_unknown(tmp_path: Path) -> None:
    database = await _open(tmp_path)
    try:
        with pytest.raises(Exception, match="会话不存在"):
            await database.set_title("missing", "随便")
    finally:
        await database.close()
