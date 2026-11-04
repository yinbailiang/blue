"""附件存储测试：内容寻址去重、级联删除、GC 与入库校验。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from blue_contracts_session import (
    SessionAttachmentBlob,
    attachment_url,
    sha256_hex,
)
from blue_service_session import SessionAttachmentError, SessionConfig, SessionService
from blue_service_session.db import SessionDatabase
from infinity_agent import Message, MultiModalContent
from tests.support import make_runtime

from blue.messages import encode_message
from blue.transport import MemoryFanout

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
"""测试用「图片」字节（内容本身不重要，只要求稳定）。"""


def _blob(data: bytes = PNG, mime: str = "image/png") -> tuple[str, str, bytes]:
    """构造（哈希, mime, 字节）三元组。"""
    return sha256_hex(data), mime, data


def _message_with_attachment(digest: str, text: str = "看图") -> str:
    """构造一条引用附件的多模态 user 消息。"""
    message = Message.user(
        [
            MultiModalContent.text_part(text),
            MultiModalContent.image_part(attachment_url(digest)),
        ]
    )
    return encode_message(message)


def test_put_and_get_attachment_round_trip(tmp_path: Path) -> None:
    """附件按哈希存取，字节原样返回。"""
    asyncio.run(_put_get(tmp_path))


async def _put_get(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        digest, mime, data = _blob()
        await db.put_attachments([(digest, mime, data)], now=100.0)
        record = await db.get_attachment(digest)
        assert record is not None
        assert record.hash == digest
        assert record.mime == mime
        assert record.size == len(data)
        assert record.data == data
    finally:
        await db.close()


def test_get_unknown_attachment_returns_none(tmp_path: Path) -> None:
    """不存在的哈希返回 None（而不是抛错）。"""
    asyncio.run(_get_unknown(tmp_path))


async def _get_unknown(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        assert await db.get_attachment("deadbeef") is None
    finally:
        await db.close()


def test_append_links_attachments_and_deduplicates(tmp_path: Path) -> None:
    """同内容只存一行，消息与附件建立映射。"""
    asyncio.run(_append_links(tmp_path))


async def _append_links(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "系统", now=100.0)
        digest, mime, data = _blob()
        payload = _message_with_attachment(digest)
        # 同一附件在两轮里各出现一次
        await db.append("s1", [payload], now=110.0, attachments=[(digest, mime, data)])
        await db.append("s1", [payload], now=120.0, attachments=[(digest, mime, data)])

        connection = db._connection_or_raise()  # pyright: ignore[reportPrivateUsage]
        cursor = await connection.execute("SELECT COUNT(*) FROM attachments")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row[0] == 1

        cursor = await connection.execute(
            "SELECT message_id, hash, position FROM message_attachments"
            " ORDER BY message_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert [tuple(row) for row in rows] == [(2, digest, 0), (3, digest, 0)]
    finally:
        await db.close()


def test_append_without_blob_still_stores_message(tmp_path: Path) -> None:
    """引用了未落库的附件时，消息照存、映射不建（不报错）。"""
    asyncio.run(_append_missing_blob(tmp_path))


async def _append_missing_blob(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "系统", now=100.0)
        payload = _message_with_attachment("f" * 64)
        result = await db.append("s1", [payload], now=110.0)
        assert result.meta.message_count == 2

        connection = db._connection_or_raise()  # pyright: ignore[reportPrivateUsage]
        cursor = await connection.execute("SELECT COUNT(*) FROM message_attachments")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row[0] == 0
    finally:
        await db.close()


def test_reset_keeps_blob_until_sweep(tmp_path: Path) -> None:
    """删会话只清映射，附件留到 GC 回收（可能被别的会话引用）。"""
    asyncio.run(_reset_then_sweep(tmp_path))


async def _reset_then_sweep(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "系统", now=100.0)
        digest, mime, data = _blob()
        await db.append(
            "s1",
            [_message_with_attachment(digest)],
            now=110.0,
            attachments=[(digest, mime, data)],
        )
        assert await db.get_attachment(digest) is not None

        assert await db.reset("s1") is True
        connection = db._connection_or_raise()  # pyright: ignore[reportPrivateUsage]
        cursor = await connection.execute("SELECT COUNT(*) FROM message_attachments")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row[0] == 0
        assert await db.get_attachment(digest) is not None

        # 宽限期内不回收
        assert await db.sweep_attachments(now=120.0, grace=3600.0) == 0
        # 宽限期外回收
        assert await db.sweep_attachments(now=10_000.0, grace=3600.0) == 1
        assert await db.get_attachment(digest) is None
    finally:
        await db.close()


def test_sweep_keeps_referenced_attachment(tmp_path: Path) -> None:
    """仍被消息引用的附件永不被回收。"""
    asyncio.run(_sweep_keeps_referenced(tmp_path))


async def _sweep_keeps_referenced(tmp_path: Path) -> None:
    db = SessionDatabase(tmp_path / "sessions.db")
    await db.connect()
    try:
        await db.ensure("s1", "系统", now=100.0)
        digest, mime, data = _blob()
        await db.append(
            "s1",
            [_message_with_attachment(digest)],
            now=110.0,
            attachments=[(digest, mime, data)],
        )
        assert await db.sweep_attachments(now=10_000.0, grace=1.0) == 0
        assert await db.get_attachment(digest) is not None
    finally:
        await db.close()


def test_schema_version_is_current_and_survives_reopen(tmp_path: Path) -> None:
    """schema 版本随新表递增；重开连接后附件仍在。"""
    asyncio.run(_schema_version(tmp_path))


async def _schema_version(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    db = SessionDatabase(path)
    await db.connect()
    try:
        connection = db._connection_or_raise()  # pyright: ignore[reportPrivateUsage]
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and row[0] == 7

        digest, mime, data = _blob()
        await db.put_attachments([(digest, mime, data)], now=100.0)
    finally:
        await db.close()

    reopened = SessionDatabase(path)
    await reopened.connect()
    try:
        record = await reopened.get_attachment(digest)
        assert record is not None and record.data == data
    finally:
        await reopened.close()


def _service() -> SessionService:
    """构造一个未启动的 SessionService（只用到校验逻辑）。"""
    return SessionService(
        config=SessionConfig(database_path="sessions.db"),
        runtime=make_runtime(MemoryFanout(), "session"),
    )


def test_decode_attachments_accepts_valid_blob() -> None:
    """合法负载解码为（哈希, mime, 字节）。"""
    digest, mime, data = _blob()
    import base64

    payload = base64.b64encode(data).decode("ascii")
    decoded = _service()._decode_attachments(  # pyright: ignore[reportPrivateUsage]
        [SessionAttachmentBlob(hash=digest, mime=mime, data=payload)]
    )
    assert decoded == [(digest, mime, data)]


def test_decode_attachments_rejects_bad_base64() -> None:
    """非法 base64 直接拒绝。"""
    with pytest.raises(SessionAttachmentError, match="base64"):
        _service()._decode_attachments(  # pyright: ignore[reportPrivateUsage]
            [SessionAttachmentBlob(hash="a" * 64, mime="image/png", data="!!!")]
        )


def test_decode_attachments_rejects_hash_mismatch() -> None:
    """哈希与内容不一致（防投毒）。"""
    import base64

    payload = base64.b64encode(PNG).decode("ascii")
    with pytest.raises(SessionAttachmentError, match="哈希"):
        _service()._decode_attachments(  # pyright: ignore[reportPrivateUsage]
            [SessionAttachmentBlob(hash="0" * 64, mime="image/png", data=payload)]
        )


def test_decode_attachments_rejects_too_many() -> None:
    """单轮附件数量超限。"""
    import base64

    digest, mime, data = _blob()
    payload = base64.b64encode(data).decode("ascii")
    blob = SessionAttachmentBlob(hash=digest, mime=mime, data=payload)
    with pytest.raises(SessionAttachmentError, match="数量"):
        _service()._decode_attachments(  # pyright: ignore[reportPrivateUsage]
            [blob] * 5
        )


def test_decode_attachments_rejects_oversize() -> None:
    """单张附件超过字节上限。"""
    import base64

    service = SessionService(
        config=SessionConfig(database_path="sessions.db", max_attachment_bytes=1024),
        runtime=make_runtime(MemoryFanout(), "session"),
    )
    big = b"x" * 2048
    payload = base64.b64encode(big).decode("ascii")
    with pytest.raises(SessionAttachmentError, match="过大"):
        service._decode_attachments(  # pyright: ignore[reportPrivateUsage]
            [
                SessionAttachmentBlob(
                    hash=sha256_hex(big), mime="image/png", data=payload
                )
            ]
        )
