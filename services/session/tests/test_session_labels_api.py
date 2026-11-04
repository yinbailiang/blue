"""分支命名与「删分支回收附件」。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from blue_contracts_session import attachment_url
from blue_service_session.db import SessionDatabase
from infinity_agent import ContentType, ImageUrl, Message, MultiModalContent

from blue.messages import encode_message
from blue.tree import ROOT_ID, Tree, TreeError


def _user(text: str) -> str:
    """一条用户消息的 JSON 文本。"""
    return encode_message(Message.user(text))


def _user_with_image(text: str, *digests: str) -> str:
    """带附件引用的用户消息。

    附件哈希必须以「完整的伪 URL 字符串」出现在消息 JSON 里（多模态 image_url），
    才会被记进 ``message_attachments`` —— 埋在 markdown 文本里不算引用。
    """
    parts = [MultiModalContent(type=ContentType.TEXT, text=text)]
    for digest in digests:
        parts.append(
            MultiModalContent(
                type=ContentType.IMAGE_URL,
                image_url=ImageUrl(url=attachment_url(digest)),
            )
        )
    return encode_message(Message.user(content=parts))


def _assistant(text: str) -> str:
    """一条助手消息的 JSON 文本。"""
    return encode_message(Message.assistant(content=text))


async def _open(tmp_path: Path) -> SessionDatabase:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    return db


def test_branch_label_is_stored_and_returned(tmp_path: Path) -> None:
    """分支名落库后随快照返回；空串表示清除。"""
    asyncio.run(_branch_label(tmp_path))


async def _branch_label(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        question = (await db.append("s1", [_user("问")], now=101.0)).nodes[0]
        first = (await db.append("s1", [_assistant("答 A")], now=102.0)).nodes[0]
        second = (
            await db.append("s1", [_assistant("答 B")], now=103.0, parent=question)
        ).nodes[0]

        assert await db.set_label("s1", first, "  宽松   版本 ") == "宽松 版本"
        assert await db.set_label("s1", second, "严谨版") == "严谨版"

        record = await db.get("s1")
        assert record is not None
        labels = {node.id: node.label for node in record.nodes}
        assert labels[first] == "宽松 版本"
        assert labels[second] == "严谨版"
        assert labels[question] == ""

        # 太长会被截断，而不是报错
        long_label = await db.set_label("s1", second, "很" * 40)
        assert len(long_label) == 24

        # 空串 = 恢复序号显示
        assert await db.set_label("s1", first, "   ") == ""
        record = await db.get("s1")
        assert record is not None
        assert {node.id: node.label for node in record.nodes}[first] == ""
    finally:
        await db.close()


def test_set_label_rejects_unknown_node(tmp_path: Path) -> None:
    """给不存在的节点起名要被拒（而不是静默写空）。"""
    asyncio.run(_label_unknown(tmp_path))


async def _label_unknown(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        try:
            await db.set_label("s1", 999, "x")
        except TreeError:
            pass
        else:
            raise AssertionError("不存在的节点应当报错")
    finally:
        await db.close()


def test_delete_branch_collects_its_attachments(tmp_path: Path) -> None:
    """删分支时把只被这条分支引用的附件一起回收，共用的附件保留。"""
    asyncio.run(_delete_collects(tmp_path))


async def _delete_collects(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        await db.ensure("s1", "sys", now=100.0)
        shared = b"shared-image-bytes"
        shared_hash = hashlib.sha256(shared).hexdigest()
        own = b"branch-only-image-bytes"
        own_hash = hashlib.sha256(own).hexdigest()

        # 两条分支各带一张图：一张两份共用，一张只有被删的那条用
        kept = (
            await db.append(
                "s1",
                [_user_with_image("分支一", shared_hash)],
                now=101.0,
                attachments=[(shared_hash, "image/png", shared)],
            )
        ).nodes[0]
        doomed = (
            await db.append(
                "s1",
                [_user_with_image("分支二", shared_hash, own_hash)],
                now=102.0,
                attachments=[
                    (shared_hash, "image/png", shared),
                    (own_hash, "image/png", own),
                ],
            )
        ).nodes[0]

        assert await db.get_attachment(shared_hash) is not None
        assert await db.get_attachment(own_hash) is not None

        removed = await db.delete_node("s1", doomed, now=103.0)
        assert removed == 1

        assert await db.get_attachment(own_hash) is None, "分支独有的附件应当被回收"
        assert await db.get_attachment(shared_hash) is not None

        record = await db.get("s1")
        assert record is not None
        tree = Tree(record.nodes, root_current=record.meta.root_current)
        assert tree.chain()[-1] == kept
        assert kept != ROOT_ID
    finally:
        await db.close()
