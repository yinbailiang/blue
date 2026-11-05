"""会话数据库测试：WAL 持久化、消息追加、回收与容量上限。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from blue_service_session.db import (
    SessionDatabase,
    SessionNotFoundError,
    SessionRecord,
)
from infinity_agent import Message, MessageRole

from blue.messages import decode_messages, encode_message
from blue.tree import Tree


def _chain(record: SessionRecord) -> list[Message]:
    """当前链上的消息（会话树里被选中的那条）。"""
    tree = Tree(record.nodes, root_current=record.meta.root_current)
    return decode_messages(node.message for node in tree.collect(tree.chain()))


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
        messages = _chain(record)
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
        result = await db.append(
            "s1",
            [encode_message(Message.user("hi"))],
            now=101.0,
        )
        assert result.meta.message_count == 2
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
        assert [message.role for message in _chain(record)] == [
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


def test_outbox_enqueue_list_claim_remove(tmp_path: Path) -> None:
    """待发送队列：先进先出、可取队首、可取消。"""
    asyncio.run(_outbox_flow(tmp_path))


async def _outbox_flow(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "sys", now=1.0)
        first = await db.enqueue_message("s1", "第一条", [], now=1.0)
        second = await db.enqueue_message("s1", "第二条", [], now=2.0)

        pending = await db.list_outbox("s1")
        assert [item.text for item in pending] == ["第一条", "第二条"]
        assert [item.position for item in pending] == [1, 2]
        assert all(item.session_id == "s1" for item in pending)
        assert all(item.state == "pending" for item in pending)

        # 认领 = 改状态而不是删行：消息还在队列里，只是标成 sending
        claimed = await db.claim_message("s1", now=3.0, stale_after=300.0)
        assert claimed is not None
        assert claimed.id == first.id
        assert claimed.state == "sending"
        assert claimed.attempts == 1
        listed = await db.list_outbox("s1")
        assert [(item.text, item.state) for item in listed] == [
            ("第一条", "sending"),
            ("第二条", "pending"),
        ]

        # 跑完 → 真的删掉
        assert await db.complete_message(claimed.id) is True
        listed = await db.list_outbox("s1")
        assert [item.text for item in listed] == ["第二条"]

        # 失败 → 留原因；重试 → 回到待发
        stuck = await db.claim_message("s1", now=4.0, stale_after=300.0)
        assert stuck is not None
        assert await db.fail_message(stuck.id, "模型超时") is True
        failed = (await db.list_outbox("s1"))[0]
        assert (failed.state, failed.error, failed.attempts) == (
            "failed",
            "模型超时",
            1,
        )
        assert failed.id == second.id
        assert await db.retry_message("s1", failed.id) is True
        recovered = (await db.list_outbox("s1"))[0]
        assert (recovered.state, recovered.error) == ("pending", None)

        # 认领后超时：自动回到待发（进程崩溃兜底）
        stalled = await db.claim_message("s1", now=10.0, stale_after=0.0)
        assert stalled is not None
        assert (await db.list_outbox("s1"))[0].state == "sending"
        reclaimed = await db.claim_message("s1", now=20.0, stale_after=5.0)
        assert reclaimed is not None
        assert reclaimed.attempts == 3
        # 上面这次认领顺带回收了「超时」的那条，再认领一次就是空队列

        # 编辑：发送中的不可改，待发的可改
        await db.complete_message(reclaimed.id)
        editable = await db.enqueue_message("s1", "待改", [], now=30.0)
        assert await db.update_message("s1", editable.id, "改过了", []) is True
        assert (await db.list_outbox("s1"))[0].text == "改过了"

        # 排序：置顶 / 上移 / 下移
        tail = await db.enqueue_message("s1", "尾巴", [], now=31.0)
        assert await db.move_message("s1", tail.id, "top") is True
        assert [item.text for item in await db.list_outbox("s1")] == [
            "尾巴",
            "改过了",
        ]
        assert await db.move_message("s1", tail.id, "down") is True
        assert [item.text for item in await db.list_outbox("s1")] == [
            "改过了",
            "尾巴",
        ]

        # 清空：发送中的那条保留
        running = await db.claim_message("s1", now=40.0, stale_after=300.0)
        assert running is not None
        assert await db.clear_outbox("s1") == 1
        remaining = await db.list_outbox("s1")
        assert [(item.text, item.state) for item in remaining] == [
            (running.text, "sending")
        ]

        # 取消不存在的项返回 False
        assert await db.remove_message("s1", second.id) is False
        assert await db.outbox_depth("s1") == 1
    finally:
        await db.close()


def test_prompt_drift_warns_once_and_keeps_stored_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """配置里的 system 提示变了：告警一次，但绝不改写已存在会话的提示。"""
    asyncio.run(_prompt_drift(tmp_path, caplog))


async def _prompt_drift(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "旧提示", now=100.0)

        with caplog.at_level(logging.WARNING, logger="blue_service_session.db"):
            await db.ensure("s1", "新提示", now=101.0)
        assert [r for r in caplog.records if "不一致" in str(r.message)]

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="blue_service_session.db"):
            record = await db.ensure("s1", "新提示", now=102.0)
        assert not [r for r in caplog.records if "不一致" in str(r.message)]
        assert _chain(record)[0].content == "旧提示"
    finally:
        await db.close()
