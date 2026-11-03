"""会话持久化：aiosqlite + WAL。

数据库只由 session 服务进程访问；一个连接 + 一把 asyncio 锁把所有写操作
串行化，读操作也走同一把锁以保持简单、可预测。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from blue_contracts_session import SessionInfo
from infinity_agent import Message

from blue.errors import BlueError
from blue.messages import encode_message

__all__ = [
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
"""


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
class SessionRecord:
    """会话完整记录；消息为 ``blue.messages`` 编码后的 JSON 文本。"""

    meta: SessionMeta
    messages: list[str]


class SessionDatabase:
    """SQLite 会话数据库。"""

    def __init__(
        self,
        path: Path,
        *,
        max_sessions: int = 512,
        idle_ttl: float = 7200.0,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions 必须 >= 1")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl 必须 > 0")
        self._path = path
        self._max_sessions = max_sessions
        self._idle_ttl = idle_ttl
        self._busy_timeout_ms = busy_timeout_ms
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
                await self._enforce_capacity_locked(connection)
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

    async def append(
        self, session_id: str, payloads: list[str], *, now: float
    ) -> SessionMeta:
        """追加消息并刷新 updated_at。"""
        if not payloads:
            meta = await self.meta(session_id)
            return meta

        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                await connection.executemany(
                    """
                    INSERT INTO messages (session_id, payload, created_at)
                    VALUES (?, ?, ?)
                    """,
                    [(session_id, payload, now) for payload in payloads],
                )
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

    async def prune(self, *, now: float) -> list[str]:
        """回收空闲会话，并按上限淘汰最久未活动会话。"""
        cutoff = now - self._idle_ttl
        removed: list[str] = []
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT session_id FROM sessions WHERE updated_at < ?",
                    (cutoff,),
                )
                removed.extend(
                    str(row["session_id"]) for row in await cursor.fetchall()
                )
                if removed:
                    await connection.executemany(
                        "DELETE FROM sessions WHERE session_id = ?",
                        [(session_id,) for session_id in removed],
                    )
                removed.extend(await self._enforce_capacity_locked(connection))
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return removed

    async def _init_schema(self, connection: aiosqlite.Connection) -> None:
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("无法读取 SQLite user_version")
        version = int(row[0])
        if version == 0:
            await connection.executescript(_SCHEMA)
            await connection.execute("PRAGMA user_version = 1")
            return
        if version != 1:
            raise RuntimeError(f"不支持的会话数据库版本：{version}")

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
        self, connection: aiosqlite.Connection
    ) -> list[str]:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM sessions")
        row = await cursor.fetchone()
        count = int(row["count"]) if row is not None else 0
        overflow = count - self._max_sessions
        if overflow <= 0:
            return []
        cursor = await connection.execute(
            """
            SELECT session_id FROM sessions
             ORDER BY updated_at ASC
             LIMIT ?
            """,
            (overflow,),
        )
        removed = [str(row["session_id"]) for row in await cursor.fetchall()]
        if removed:
            await connection.executemany(
                "DELETE FROM sessions WHERE session_id = ?",
                [(session_id,) for session_id in removed],
            )
        return removed

    def _connection_or_raise(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("会话数据库尚未连接")
        return self._connection
