"""会话持久化：aiosqlite + WAL。

数据库只由 session 服务进程访问；一个连接 + 一把 asyncio 锁把所有写操作
串行化，读操作也走同一把锁以保持简单、可预测。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
from blue_contracts_session import SessionInfo, collect_attachment_hashes
from infinity_agent import Message

from blue.errors import BlueError
from blue.messages import encode_message

logger = logging.getLogger(__name__)

__all__ = [
    "AttachmentRecord",
    "PruneResult",
    "SessionDatabase",
    "SessionMeta",
    "SessionNotFoundError",
    "SessionRecord",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
    ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS attachments (
    hash TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS message_attachments (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    hash TEXT NOT NULL REFERENCES attachments(hash),
    position INTEGER NOT NULL,
    PRIMARY KEY (message_id, position)
);

CREATE INDEX IF NOT EXISTS idx_message_attachments_hash
    ON message_attachments(hash);
"""

_SCHEMA_VERSION = 2
"""schema 版本；所有建表语句都是 ``IF NOT EXISTS``，老库按需补表即可。"""


class SessionNotFoundError(BlueError):
    """会话不存在（可能已被重置或过期回收）。"""


@dataclass(frozen=True)
class SessionMeta:
    """会话元信息。"""

    session_id: str
    created_at: float
    updated_at: float
    steps: int
    message_count: int


@dataclass(frozen=True)
class AttachmentRecord:
    """一条附件：内容哈希、元信息与原始字节。"""

    hash: str
    mime: str
    size: int
    data: bytes


@dataclass(frozen=True)
class SessionRecord:
    """会话完整记录；消息为 ``blue.messages`` 编码后的 JSON 文本。"""

    meta: SessionMeta
    messages: list[str]


@dataclass(frozen=True)
class PruneResult:
    """一次回收的结果：过期回收的会话与超容量淘汰的会话分开记账。"""

    expired: list[str] = field(default_factory=list[str])
    """超过 ``idle_ttl`` 被回收的会话。"""

    evicted: list[str] = field(default_factory=list[str])
    """因超出 ``max_sessions`` 被淘汰的最久未活动会话。"""

    @property
    def removed(self) -> list[str]:
        """本次删除的全部会话 id（过期在前，容量淘汰在后）。"""
        return [*self.expired, *self.evicted]

    @property
    def total(self) -> int:
        """本次删除的会话数。"""
        return len(self.expired) + len(self.evicted)


class SessionDatabase:
    """SQLite 会话数据库。"""

    def __init__(
        self,
        path: Path,
        *,
        max_sessions: int = 512,
        idle_ttl: float = 7200.0,
        busy_timeout_ms: int = 5000,
        active_grace: float = 0.0,
        prune_batch_size: int = 200,
        enforce_capacity: bool = False,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions 必须 >= 1")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl 必须 > 0")
        if active_grace < 0:
            raise ValueError("active_grace 不能为负")
        if prune_batch_size < 1:
            raise ValueError("prune_batch_size 必须 >= 1")
        self._path = path
        self._max_sessions = max_sessions
        self._idle_ttl = idle_ttl
        self._busy_timeout_ms = busy_timeout_ms
        self._active_grace = active_grace
        self._prune_batch_size = prune_batch_size
        self._enforce_capacity = enforce_capacity
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """数据库路径。"""
        return self._path

    async def connect(self) -> None:
        """打开连接、启用 WAL 并初始化 schema。"""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = await aiosqlite.connect(self._path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA synchronous=NORMAL")
            await connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            await connection.execute("PRAGMA foreign_keys=ON")
            await self._init_schema(connection)
        except Exception:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        """关闭连接。"""
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    async def ensure(
        self, session_id: str, system_prompt: str, *, now: float
    ) -> SessionRecord:
        """取会话；不存在时创建并写入 system 消息。"""
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO sessions
                        (session_id, created_at, updated_at, steps, message_count)
                    VALUES (?, ?, ?, 0, 0)
                    """,
                    (session_id, now, now),
                )
                created = cursor.rowcount > 0
                if created and system_prompt:
                    payload = encode_message(Message.system(system_prompt))
                    await connection.execute(
                        """
                        INSERT INTO messages (session_id, payload, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, payload, now),
                    )
                    await connection.execute(
                        """
                        UPDATE sessions
                           SET message_count = 1
                         WHERE session_id = ?
                        """,
                        (session_id,),
                    )
                await self._enforce_capacity_locked(connection, now=now)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            record = await self._record_locked(session_id)
        if record is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return record

    async def get(self, session_id: str) -> SessionRecord | None:
        """读取会话完整记录。"""
        async with self._lock:
            return await self._record_locked(session_id)

    async def put_attachments(
        self, blobs: Iterable[tuple[str, str, bytes]], *, now: float
    ) -> None:
        """按内容哈希写入附件（已存在则只刷新 last_seen_at）。"""
        rows = [
            (digest, mime, len(data), data, now, now) for digest, mime, data in blobs
        ]
        if not rows:
            return
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.executemany(
                """
                INSERT INTO attachments
                       (hash, mime, size, data, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                rows,
            )
            await connection.commit()

    async def get_attachment(self, digest: str) -> AttachmentRecord | None:
        """按内容哈希取附件；不存在时返回 ``None``。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                "SELECT hash, mime, size, data FROM attachments WHERE hash = ?",
                (digest,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return AttachmentRecord(
            hash=str(row[0]), mime=str(row[1]), size=int(row[2]), data=bytes(row[3])
        )

    async def sweep_attachments(self, *, now: float, grace: float) -> int:
        """回收不再被任何消息引用、且超过宽限期的附件；返回删除条数。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                DELETE FROM attachments
                 WHERE last_seen_at < ?
                   AND hash NOT IN (SELECT hash FROM message_attachments)
                """,
                (now - grace,),
            )
            removed = int(cursor.rowcount or 0)
            await cursor.close()
            await connection.commit()
        return max(0, removed)

    async def append(
        self,
        session_id: str,
        payloads: list[str],
        *,
        now: float,
        attachments: Iterable[tuple[str, str, bytes]] = (),
    ) -> SessionMeta:
        """追加消息（可同时落库它们引用的附件）并刷新 updated_at。"""
        blobs = list(attachments)
        if not payloads and not blobs:
            meta = await self.meta(session_id)
            return meta

        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                await self._upsert_attachments_locked(blobs, now=now)
                for payload in payloads:
                    cursor = await connection.execute(
                        """
                        INSERT INTO messages (session_id, payload, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, payload, now),
                    )
                    message_id = cursor.lastrowid
                    await cursor.close()
                    if message_id is not None:
                        await self._link_attachments_locked(int(message_id), payload)
                if payloads:
                    await connection.execute(
                        """
                        UPDATE sessions
                           SET updated_at = ?,
                               message_count = message_count + ?
                         WHERE session_id = ?
                        """,
                        (now, len(payloads), session_id),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            updated = await self._meta_locked(session_id)
        if updated is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return updated

    async def meta(self, session_id: str) -> SessionMeta:
        """读取会话元信息。

        :raises SessionNotFoundError: 会话不存在。
        """
        async with self._lock:
            meta = await self._meta_locked(session_id)
        if meta is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return meta

    async def update(
        self,
        session_id: str,
        *,
        steps: int | None,
        touch: bool,
        now: float,
    ) -> SessionMeta:
        """更新步数与活动时间。"""
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                if steps is not None and touch:
                    await connection.execute(
                        """
                        UPDATE sessions
                           SET steps = ?, updated_at = ?
                         WHERE session_id = ?
                        """,
                        (steps, now, session_id),
                    )
                elif steps is not None:
                    await connection.execute(
                        "UPDATE sessions SET steps = ? WHERE session_id = ?",
                        (steps, session_id),
                    )
                elif touch:
                    await connection.execute(
                        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            updated = await self._meta_locked(session_id)
        if updated is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return updated

    async def reset(self, session_id: str) -> bool:
        """删除会话及其全部消息；返回是否确实存在。"""
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )
                removed = cursor.rowcount > 0
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            return removed

    async def state(self) -> list[SessionInfo]:
        """按最后更新时间倒序返回全部会话元信息。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                SELECT session_id, message_count, updated_at
                  FROM sessions
                 ORDER BY updated_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            SessionInfo(
                session_id=str(row["session_id"]),
                message_count=int(row["message_count"]),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    async def prune(self, *, now: float) -> PruneResult:
        """回收空闲会话，并按上限淘汰最久未活动会话。

        过期的先删（最多 ``prune_batch_size`` 个，避免一次长事务锁库），
        再按容量淘汰；容量淘汰会跳过 ``active_grace`` 秒内活动过的会话，
        以免误删正在跑长任务的对话。
        """
        cutoff = now - self._idle_ttl
        expired: list[str] = []
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT session_id FROM sessions
                     WHERE updated_at < ?
                     ORDER BY updated_at ASC
                     LIMIT ?
                    """,
                    (cutoff, self._prune_batch_size),
                )
                expired = [str(row["session_id"]) for row in await cursor.fetchall()]
                await cursor.close()
                if expired:
                    await connection.executemany(
                        "DELETE FROM sessions WHERE session_id = ?",
                        [(session_id,) for session_id in expired],
                    )
                evicted = await self._enforce_capacity_locked(connection, now=now)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return PruneResult(expired=expired, evicted=evicted)

    async def _init_schema(self, connection: aiosqlite.Connection) -> None:
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("无法读取 SQLite user_version")
        version = int(row[0])
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"会话数据库版本 {version} 高于本程序支持的 {_SCHEMA_VERSION}"
            )
        if version == _SCHEMA_VERSION:
            return
        # 建表语句全是 IF NOT EXISTS：老库只需补缺失的表，不需要重写数据
        await connection.executescript(_SCHEMA)
        await connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    async def _record_locked(self, session_id: str) -> SessionRecord | None:
        meta = await self._meta_locked(session_id)
        if meta is None:
            return None
        connection = self._connection_or_raise()
        cursor = await connection.execute(
            """
            SELECT payload FROM messages
             WHERE session_id = ?
             ORDER BY id
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return SessionRecord(
            meta=meta,
            messages=[str(row["payload"]) for row in rows],
        )

    async def _meta_locked(self, session_id: str) -> SessionMeta | None:
        connection = self._connection_or_raise()
        cursor = await connection.execute(
            """
            SELECT session_id, created_at, updated_at, steps, message_count
              FROM sessions
             WHERE session_id = ?
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return SessionMeta(
            session_id=str(row["session_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            steps=int(row["steps"]),
            message_count=int(row["message_count"]),
        )

    async def _enforce_capacity_locked(
        self, connection: aiosqlite.Connection, *, now: float
    ) -> list[str]:
        """超出 ``max_sessions`` 时淘汰最久未活动的会话。

        * ``enforce_capacity=False``（自动回收关闭）时直接跳过；
        * ``active_grace`` 秒内活动过的会话受保护，宁可临时超上限也不误删；
        * 单次最多淘汰 ``prune_batch_size`` 个，避免一个事务删太多。
        """
        if not self._enforce_capacity:
            return []
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM sessions")
        row = await cursor.fetchone()
        await cursor.close()
        count = int(row["count"]) if row is not None else 0
        overflow = min(count - self._max_sessions, self._prune_batch_size)
        if overflow <= 0:
            return []
        cursor = await connection.execute(
            """
            SELECT session_id FROM sessions
             WHERE updated_at <= ?
             ORDER BY updated_at ASC
             LIMIT ?
            """,
            (now - self._active_grace, overflow),
        )
        removed = [str(row["session_id"]) for row in await cursor.fetchall()]
        await cursor.close()
        if not removed:
            return []
        if len(removed) < overflow:
            logger.debug(
                "有 %s 个会话在保护期内（active_grace=%ss），本次只淘汰 %s 个",
                overflow - len(removed),
                self._active_grace,
                len(removed),
            )
        await connection.executemany(
            "DELETE FROM sessions WHERE session_id = ?",
            [(session_id,) for session_id in removed],
        )
        return removed

    async def _upsert_attachments_locked(
        self, blobs: Iterable[tuple[str, str, bytes]], *, now: float
    ) -> None:
        """事务内写附件（幂等：同哈希只刷新 last_seen_at）。"""
        rows = [
            (digest, mime, len(data), data, now, now) for digest, mime, data in blobs
        ]
        if not rows:
            return
        connection = self._connection_or_raise()
        await connection.executemany(
            """
            INSERT INTO attachments (hash, mime, size, data, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            rows,
        )

    async def _link_attachments_locked(self, message_id: int, payload: str) -> None:
        """把消息里引用的附件登记到映射表；未落库的哈希只告警不关联。"""
        hashes = collect_attachment_hashes(payload)
        if not hashes:
            return
        connection = self._connection_or_raise()
        placeholders = ",".join("?" * len(hashes))
        cursor = await connection.execute(
            f"SELECT hash FROM attachments WHERE hash IN ({placeholders})",
            hashes,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        known = {str(row[0]) for row in rows}
        missing = [digest for digest in hashes if digest not in known]
        if missing:
            logger.warning("消息引用了未落库的附件，已忽略关联：%s", missing)
        links = [
            (message_id, digest, position)
            for position, digest in enumerate(hashes)
            if digest in known
        ]
        if links:
            await connection.executemany(
                """
                INSERT OR REPLACE INTO message_attachments (message_id, hash, position)
                VALUES (?, ?, ?)
                """,
                links,
            )

    def _connection_or_raise(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("会话数据库尚未连接")
        return self._connection
