"""会话树存储测试：分支追加、切换、删除与线性老库迁移。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from blue_service_session.db import SessionDatabase, SessionNodeRecord, SessionRecord
from infinity_agent import Message, MessageRole

from blue.messages import decode_messages, encode_message
from blue.tree import ROOT_ID, Tree, TreeError


def _user(text: str) -> str:
    """一条用户消息的落库负载。"""
    return encode_message(Message.user(text))


def _assistant(text: str) -> str:
    """一条助手消息的落库负载。"""
    return encode_message(Message.assistant(content=text))


def _tree(record: SessionRecord) -> Tree[SessionNodeRecord]:
    """会话记录对应的树视图。"""
    return Tree(record.nodes, root_current=record.meta.root_current)


def _chain(record: SessionRecord) -> list[Message]:
    """当前链上的消息。"""
    tree = _tree(record)
    return decode_messages(node.message for node in tree.collect(tree.chain()))


async def _open(tmp_path: Path) -> SessionDatabase:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    return db


def test_system_message_is_the_first_root_branch(tmp_path: Path) -> None:
    """system 消息就是根的第一个孩子，根指针指向它。"""
    asyncio.run(_system_message(tmp_path))


async def _system_message(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        record = await db.ensure("s1", "系统提示", now=100.0)
        assert [node.parent for node in record.nodes] == [ROOT_ID]
        assert record.meta.root_current == record.nodes[0].id
        assert _tree(record).chain() == [record.nodes[0].id]
    finally:
        await db.close()


def test_append_hangs_on_the_current_leaf(tmp_path: Path) -> None:
    """不带 parent 的追加挂在当前链末端，并成为新的末端。"""
    asyncio.run(_append_on_leaf(tmp_path))


async def _append_on_leaf(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        first = await db.append("s1", [_user("第一问")], now=101.0)
        second = await db.append("s1", [_assistant("第一答")], now=102.0)
        record = await db.get("s1")
        assert record is not None
        assert second.nodes[0] > first.nodes[0]
        assert _tree(record).chain() == [
            record.nodes[0].id,
            first.nodes[0],
            second.nodes[0],
        ]
        assert [message.content for message in _chain(record)] == [
            "sys",
            "第一问",
            "第一答",
        ]
    finally:
        await db.close()


def test_append_with_explicit_parent_opens_a_branch(tmp_path: Path) -> None:
    """在同一个父节点下追加 → 新分支，被选中，旧分支原样保留。"""
    asyncio.run(_open_branch(tmp_path))


async def _open_branch(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问题")], now=101.0)).nodes[0]
        first_answer = (await db.append("s1", [_assistant("答案 A")], now=102.0)).nodes[
            0
        ]
        second = await db.append(
            "s1", [_assistant("答案 B")], now=103.0, parent=question
        )
        record = await db.get("s1")
        assert record is not None
        tree = _tree(record)
        assert tree.children(question) == [first_answer, second.nodes[0]]
        assert tree.get(question) is not None
        assert tree.get(question).current == second.nodes[0]  # type: ignore[union-attr]
        assert tree.chain()[-1] == second.nodes[0]
        # 两条回答都还在库里，只是有一条没被选中
        assert record.meta.message_count == 4
    finally:
        await db.close()


def test_switch_branch_only_moves_the_pointer(tmp_path: Path) -> None:
    """切换分支：链变了，节点与其内容一条都没少。"""
    asyncio.run(_switch_branch(tmp_path))


async def _switch_branch(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问题")], now=101.0)).nodes[0]
        first = (await db.append("s1", [_assistant("答案 A")], now=102.0)).nodes[0]
        second = (
            await db.append("s1", [_assistant("答案 B")], now=103.0, parent=question)
        ).nodes[0]
        before = await db.get("s1")
        assert before is not None

        await db.set_current("s1", first)
        record = await db.get("s1")
        assert record is not None
        assert _tree(record).chain()[-1] == first
        assert [message.content for message in _chain(record)][-1] == "答案 A"
        assert record.meta.message_count == before.meta.message_count
        assert [node.id for node in record.nodes] == [node.id for node in before.nodes]

        await db.set_current("s1", second)
        again = await db.get("s1")
        assert again is not None
        assert _tree(again).chain()[-1] == second
        assert question in _tree(again).chain()
    finally:
        await db.close()


def test_switch_to_unknown_node_is_rejected(tmp_path: Path) -> None:
    """切到不存在的节点必须显式失败，不能悄悄改指针。"""
    asyncio.run(_switch_unknown(tmp_path))


async def _switch_unknown(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        with pytest.raises(TreeError):
            await db.set_current("s1", 999)
    finally:
        await db.close()


def test_delete_branch_falls_back_to_the_newest_remaining(tmp_path: Path) -> None:
    """删掉当前分支：父节点回落到剩下的最新孩子。"""
    asyncio.run(_delete_branch(tmp_path))


async def _delete_branch(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问题")], now=101.0)).nodes[0]
        first = (await db.append("s1", [_assistant("答案 A")], now=102.0)).nodes[0]
        second = (
            await db.append("s1", [_assistant("答案 B")], now=103.0, parent=question)
        ).nodes[0]

        removed = await db.delete_node("s1", second, now=104.0)
        assert removed == 1
        record = await db.get("s1")
        assert record is not None
        tree = _tree(record)
        assert tree.children(question) == [first]
        assert tree.chain()[-1] == first
        assert record.meta.message_count == 3

        # 删掉最后一个孩子：父节点不再选中任何分支
        await db.delete_node("s1", first, now=105.0)
        empty = await db.get("s1")
        assert empty is not None
        assert _tree(empty).chain()[-1] == question
        assert _tree(empty).get(question).current is None  # type: ignore[union-attr]
    finally:
        await db.close()


def test_delete_removes_the_whole_subtree(tmp_path: Path) -> None:
    """删一条分支 = 该节点连同后续整串回答一起消失。"""
    asyncio.run(_delete_subtree(tmp_path))


async def _delete_subtree(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问题")], now=101.0)).nodes[0]
        answer = (await db.append("s1", [_assistant("答案")], now=102.0)).nodes[0]
        await db.append("s1", [_user("追问")], now=103.0)
        removed = await db.delete_node("s1", question, now=104.0)
        assert removed == 3
        record = await db.get("s1")
        assert record is not None
        assert [node.id for node in record.nodes] == [record.nodes[0].id]
        assert record.meta.root_current == record.nodes[0].id
        assert answer not in [node.id for node in record.nodes]
    finally:
        await db.close()


def test_top_level_branches_switch_through_the_root(tmp_path: Path) -> None:
    """顶层也能开分支：parent=0 表示另一个开场白，根指针负责选中。"""
    asyncio.run(_root_branches(tmp_path))


async def _root_branches(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        # 没有 system 提示时第一条消息就挂在根上，顶层同样能开分支
        await db.ensure("s1", "", now=100.0)
        first = (await db.append("s1", [_user("开场 A")], now=101.0)).nodes[0]
        second = (
            await db.append("s1", [_user("开场 B")], now=102.0, parent=ROOT_ID)
        ).nodes[0]
        record = await db.get("s1")
        assert record is not None
        assert record.meta.root_current == second
        assert _tree(record).children(ROOT_ID) == [first, second]
        assert _tree(record).chain() == [second]

        await db.set_current("s1", first)
        switched = await db.get("s1")
        assert switched is not None
        assert _tree(switched).chain() == [first]

        await db.delete_node("s1", second, now=103.0)
        back = await db.get("s1")
        assert back is not None
        assert back.meta.root_current == first
        assert _tree(back).chain() == [first]
    finally:
        await db.close()


def test_append_under_unknown_parent_writes_nothing(tmp_path: Path) -> None:
    """挂载点过期就该失败，而且不能留下半条链。"""
    asyncio.run(_append_unknown_parent(tmp_path))


async def _append_unknown_parent(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        before = await db.get("s1")
        assert before is not None
        with pytest.raises(TreeError):
            await db.append("s1", [_user("孤儿")], now=101.0, parent=999)
        after = await db.get("s1")
        assert after is not None
        assert after.meta.message_count == before.meta.message_count
    finally:
        await db.close()


def test_tree_survives_reopen(tmp_path: Path) -> None:
    """树形指针是持久化数据：重开连接后分支结构照旧。"""
    asyncio.run(_reopen(tmp_path))


async def _reopen(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    db = SessionDatabase(path)
    await db.connect()
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问题")], now=101.0)).nodes[0]
        await db.append("s1", [_assistant("答案 A")], now=102.0)
        branch = (
            await db.append("s1", [_assistant("答案 B")], now=103.0, parent=question)
        ).nodes[0]
    finally:
        await db.close()

    reopened = SessionDatabase(path)
    await reopened.connect()
    try:
        record = await reopened.get("s1")
        assert record is not None
        tree = _tree(record)
        assert tree.get(question) is not None
        assert tree.get(question).current == branch  # type: ignore[union-attr]
        assert [message.content for message in _chain(record)][-1] == "答案 B"
    finally:
        await reopened.close()


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


def test_linear_database_is_migrated_to_a_single_chain(tmp_path: Path) -> None:
    """老库（schema 3）升级：线性历史原样折成一条单链。"""
    asyncio.run(_migrate_linear(tmp_path))


async def _migrate_linear(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    payloads = [_user("第一问"), _assistant("第一答"), _user("第二问")]
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(_V3_SCHEMA)
        await connection.execute(
            """
            INSERT INTO sessions
                   (session_id, created_at, updated_at, steps, message_count)
            VALUES ('s1', 100.0, 101.0, 0, ?)
            """,
            (len(payloads),),
        )
        for offset, payload in enumerate(payloads):
            await connection.execute(
                """
                INSERT INTO messages (session_id, payload, created_at)
                VALUES ('s1', ?, ?)
                """,
                (payload, 100.0 + offset),
            )
        await connection.execute("PRAGMA user_version = 3")
        await connection.commit()
    finally:
        await connection.close()

    db = SessionDatabase(path)
    await db.connect()
    try:
        record = await db.get("s1")
        assert record is not None
        tree = _tree(record)
        ids = [node.id for node in record.nodes]
        # 顺序不变、父链相邻、根指针指向第一条
        assert tree.chain() == ids
        assert [node.parent for node in record.nodes] == [ROOT_ID, *ids[:-1]]
        assert [node.current for node in record.nodes] == [*ids[1:], None]
        assert record.meta.root_current == ids[0]
        assert [message.content for message in _chain(record)] == [
            "第一问",
            "第一答",
            "第二问",
        ]
        assert [message.role for message in _chain(record)] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.USER,
        ]

        # 迁移后还能照常在其中一条消息下开新分支
        branch = (
            await db.append("s1", [_assistant("改问")], now=200.0, parent=ids[0])
        ).nodes[0]
        after = await db.get("s1")
        assert after is not None
        assert _tree(after).children(ids[0]) == [ids[1], branch]
        assert _tree(after).chain()[-1] == branch
    finally:
        await db.close()

    connection = await aiosqlite.connect(path)
    try:
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row[0] == 5
        cursor = await connection.execute("PRAGMA table_info(messages)")
        columns = {str(item[1]) for item in await cursor.fetchall()}
        await cursor.close()
        assert {"parent_id", "current_id"} <= columns
    finally:
        await connection.close()
