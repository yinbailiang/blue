"""会话持久化：aiosqlite + WAL。

数据库只由 session 服务进程访问；一个连接 + 一把 asyncio 锁把所有写操作
串行化，读操作也走同一把锁以保持简单、可预测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import aiosqlite
from blue_contracts_session import (
    OutboxState,
    QueuedMessage,
    SessionAttachment,
    SessionInfo,
    collect_attachment_hashes,
)
from infinity_agent import Message, MessageRole

from blue.errors import BlueError
from blue.messages import (
    PREVIEW_LIMIT,
    TITLE_LIMIT,
    condense,
    decode_message,
    decode_messages,
    derive_title,
    encode_message,
    message_text,
)
from blue.tree import ROOT_ID, Tree, pick_current

logger = logging.getLogger(__name__)

__all__ = [
    "LABEL_LIMIT",
    "AppendResult",
    "AttachmentRecord",
    "PruneResult",
    "SessionDatabase",
    "SessionMeta",
    "SessionNodeRecord",
    "SessionNotFoundError",
    "SessionRecord",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    root_current_id INTEGER,
    title TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    current_id INTEGER,
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

CREATE TABLE IF NOT EXISTS session_outbox (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    claimed_at REAL,
    position REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_session_outbox_session
    ON session_outbox(session_id, position);
"""

SNIPPET_CHARS = 160
"""搜索命中片段的字符数上限。"""

LABEL_LIMIT = 24
"""分支名长度上限（字）。"""

_SCHEMA_VERSION = 7
"""schema 版本；所有建表语句都是 ``IF NOT EXISTS``，老库按需补列/补表即可。

版本 6 把待发送队列升级成**有状态**的：``state``（pending/sending/failed）、
``attempts``、``error``、``claimed_at``（认领时刻，超时回收用）、``position``
（显式排序键，支持重排）。老库按 ``created_at`` 回填 position，顺序不变。

版本 5 给 ``sessions`` 加了 ``title`` / ``preview``：会话标题不再是浏览器里的
本地状态，而是服务端按首条用户消息自动生成、也可显式改名；``preview`` 是最后
一条用户消息的摘要，侧栏直接拿来显示。

版本 4 引入对话树：``messages`` 多了 ``parent_id`` / ``current_id``，
``sessions`` 多了 ``root_current_id``。老库升级时按「一条单链」回填：
前一条是父、后一条是选中分支，历史顺序与升级前完全一致。
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
    root_current: int | None = None
    title: str = ""
    preview: str = ""
    """根当前选中的顶层分支（第一条消息那一层）。"""


@dataclass(frozen=True)
class AttachmentRecord:
    """一条附件：内容哈希、元信息与原始字节。"""

    hash: str
    mime: str
    size: int
    data: bytes


@dataclass(frozen=True)
class SessionNodeRecord:
    """会话树里的一个节点：一条消息 + 它在树里的位置。

    ``parent`` 为 :data:`blue.tree.ROOT_ID` 表示挂在会话根上；``current`` 是
    该节点当前选中的子节点，``None`` 表示它是叶子。``label`` 是这个节点作为
    「一条分支的起点」时的分支名，空串表示界面用序号显示。
    """

    id: int
    parent: int
    current: int | None
    message: str
    """``blue.messages`` 编码后的消息 JSON 文本。"""

    created_at: float
    label: str = ""


@dataclass(frozen=True)
class SessionRecord:
    """会话完整记录：整棵树（不只是当前链）。"""

    meta: SessionMeta
    nodes: list[SessionNodeRecord] = field(default_factory=list[SessionNodeRecord])


@dataclass(frozen=True)
class AppendResult:
    """一次追加的结果：新元信息 + 新节点的 id（按插入顺序）。"""

    meta: SessionMeta
    nodes: list[int] = field(default_factory=list[int])


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
        self._prompt_warned: set[str] = set()

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
                    inserted = await connection.execute(
                        """
                        INSERT INTO messages
                            (session_id, parent_id, payload, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, ROOT_ID, payload, now),
                    )
                    system_id = inserted.lastrowid
                    await inserted.close()
                    # system 消息就是根的第一条分支：之后所有消息都挂在它下面
                    await connection.execute(
                        """
                        UPDATE sessions
                           SET message_count = 1,
                               root_current_id = ?
                         WHERE session_id = ?
                        """,
                        (system_id, session_id),
                    )
                elif not created and system_prompt:
                    await self._warn_prompt_drift_locked(
                        connection, session_id, system_prompt
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

    async def _warn_prompt_drift_locked(
        self,
        connection: aiosqlite.Connection,
        session_id: str,
        system_prompt: str,
    ) -> None:
        """已存在的会话不会回写 system 提示：配置变了就告警一次，别让它静默失效。

        刻意**不改**库里的提示：历史消息都是在那条提示下产生的，悄悄换掉会让
        旧对话的语义前后矛盾。需要新提示就新建会话。
        """
        if session_id in self._prompt_warned:
            return
        cursor = await connection.execute(
            "SELECT payload FROM messages"
            " WHERE session_id = ? AND parent_id = ? ORDER BY id LIMIT 1",
            (session_id, ROOT_ID),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None:
            return
        stored = decode_message(str(row["payload"]))
        body = stored.content if isinstance(stored.content, str) else ""
        if body == system_prompt:
            return
        self._prompt_warned.add(session_id)
        logger.warning(
            "会话 %s 的 system 提示与当前配置不一致（库内 %s 字符 / 配置 %s 字符）："
            "已存在的会话沿用建会话时的提示，需要新提示请新建会话",
            session_id,
            len(body),
            len(system_prompt),
        )

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

    async def _collect_orphan_attachments_locked(
        self, connection: aiosqlite.Connection
    ) -> int:
        """删掉没有任何消息引用的附件（调用方负责事务）。"""
        cursor = await connection.execute(
            """
            DELETE FROM attachments
             WHERE hash NOT IN (SELECT hash FROM message_attachments)
            """
        )
        removed = int(cursor.rowcount or 0)
        await cursor.close()
        return max(0, removed)

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
        parent: int | None = None,
    ) -> AppendResult:
        """追加消息（可同时落库它们引用的附件）并刷新 updated_at。

        这批消息自动串成一条链：第 k 条挂在第 k-1 条下，所以
        ``assistant + 它的工具结果`` 是一次原子提交的一整段分支。
        ``parent`` 只决定第一个节点的挂载点；``None`` 表示当前链末端（正常发言），
        显式给出则是在那条消息下开新分支（编辑 / 重试）。

        :raises SessionNotFoundError: 会话不存在
        :raises TreeError: 挂载点不存在（已过期或被删）
        """
        blobs = list(attachments)
        if not payloads and not blobs:
            meta = await self.meta(session_id)
            return AppendResult(meta=meta)

        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                await self._upsert_attachments_locked(blobs, now=now)
                mount = await self._resolve_mount_locked(connection, meta, parent)
                new_ids: list[int] = []
                previous = mount
                title = meta.title
                preview = meta.preview
                for payload in payloads:
                    title, preview = _refresh_labels(payload, title, preview)
                    inserted = await connection.execute(
                        """
                        INSERT INTO messages
                            (session_id, parent_id, payload, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, previous, payload, now),
                    )
                    row_id = inserted.lastrowid
                    await inserted.close()
                    if row_id is None:
                        raise RuntimeError("插入消息后拿不到 rowid")
                    node_id = int(row_id)
                    # 新分支立刻成为父节点的当前分支
                    await self._select_child_locked(
                        connection, session_id, previous, node_id
                    )
                    await self._link_attachments_locked(node_id, payload)
                    previous = node_id
                    new_ids.append(node_id)
                if payloads:
                    await connection.execute(
                        """
                        UPDATE sessions
                           SET updated_at = ?,
                               message_count = message_count + ?,
                               title = ?,
                               preview = ?
                         WHERE session_id = ?
                        """,
                        (now, len(payloads), title, preview, session_id),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            updated = await self._meta_locked(session_id)
        if updated is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return AppendResult(meta=updated, nodes=new_ids)

    async def set_title(self, session_id: str, title: str) -> str:
        """设置会话标题；空字符串表示恢复自动标题（按首条用户消息重算）。

        :raises SessionNotFoundError: 会话不存在
        """
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                cleaned = condense(title, limit=TITLE_LIMIT) if title.strip() else ""
                if not cleaned:
                    cleaned = await self._derive_title_locked(connection, session_id)
                await connection.execute(
                    "UPDATE sessions SET title = ? WHERE session_id = ?",
                    (cleaned, session_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return cleaned

    async def _derive_title_locked(
        self, connection: aiosqlite.Connection, session_id: str
    ) -> str:
        """按「当前链」上第一条用户消息重算标题。"""
        del connection
        record = await self._record_locked(session_id)
        if record is None:
            return ""
        tree: Tree[SessionNodeRecord] = Tree(
            record.nodes, root_current=record.meta.root_current
        )
        messages = decode_messages(
            (node.message for node in tree.collect(tree.chain())),
        )
        return derive_title(messages)

    async def set_current(self, session_id: str, node_id: int) -> SessionMeta:
        """切换当前分支：把该节点的父节点指针指到它。

        只动指针，不动任何消息——这正是「分支切换不丢历史」的原因。

        :raises SessionNotFoundError: 会话不存在
        :raises TreeError: 节点不存在
        """
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                tree = await self._tree_locked(connection, meta)
                node = tree.require(node_id)
                await self._select_child_locked(
                    connection, session_id, node.parent, node.id
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
            updated = await self._meta_locked(session_id)
        if updated is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        return updated

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 5,
        roles: Iterable[str] = ("user", "assistant", "tool"),
    ) -> list[tuple[int, str, int, int, str]]:
        """在当前链上按子串搜索，返回 ``(id, role, 序号, 字符数, 片段)``。

        用 SQL ``LIKE`` 先缩小范围（SQLite 用 C 跑），再在 Python 里定位命中处、
        截片段。只搜**当前链**：兄弟分支是"另一个版本的历史"，不该混进来。
        """
        wanted = [role for role in roles if role]
        async with self._lock:
            connection = self._connection_or_raise()
            record = await self._record_locked(session_id)
            if record is None:
                raise SessionNotFoundError(f"会话不存在：{session_id}")
            chain = Tree(record.nodes, root_current=record.meta.root_current).chain()
            order = {node_id: index for index, node_id in enumerate(chain)}
            if not chain:
                return []
            placeholders = ",".join("?" * len(chain))
            sql = f"""
                SELECT id, payload FROM messages
                 WHERE session_id = ? AND id IN ({placeholders})
                   AND payload LIKE ? ESCAPE '\\'
            """
            params: list[object] = [session_id, *chain, f"%{_like_escape(query)}%"]
            if wanted:
                role_placeholders = ",".join("?" * len(wanted))
                sql += f" AND json_extract(payload, '$.role') IN ({role_placeholders})"
                params.extend(wanted)
            sql += " ORDER BY id"
            cursor = await connection.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()

        needle = query.lower()
        hits: list[tuple[int, str, int, int, str]] = []
        for row in rows:
            node_id = int(row["id"])
            message = decode_message(str(row["payload"]))
            role = str(getattr(message.role, "value", message.role)).lower()
            text = message_text(message)
            position = text.lower().find(needle)
            if position < 0:
                # LIKE 可能命中 JSON 里的字段名（如 tool_call 参数），正文没命中就跳过
                continue
            hits.append(
                (
                    node_id,
                    role,
                    order.get(node_id, 0),
                    len(text),
                    _snippet(text, position, len(query)),
                )
            )
            if len(hits) >= max(1, limit):
                break
        return hits

    async def read(
        self, session_id: str, ids: Iterable[int], *, max_chars: int = 4000
    ) -> tuple[list[tuple[int, str, int, str, bool]], list[int]]:
        """按 id 读原文；返回 ``(messages, missing)``。"""
        wanted = [int(node_id) for node_id in ids]
        limit = max(200, min(max_chars, 200_000))
        async with self._lock:
            connection = self._connection_or_raise()
            if not wanted:
                return [], []
            placeholders = ",".join("?" * len(wanted))
            cursor = await connection.execute(
                "SELECT id, payload FROM messages"
                f" WHERE session_id = ? AND id IN ({placeholders})",
                [session_id, *wanted],
            )
            rows = await cursor.fetchall()
            await cursor.close()

        found: dict[int, str] = {int(row["id"]): str(row["payload"]) for row in rows}
        messages: list[tuple[int, str, int, str, bool]] = []
        missing: list[int] = []
        for node_id in wanted:
            payload = found.get(node_id)
            if payload is None:
                missing.append(node_id)
                continue
            message = decode_message(payload)
            role = str(getattr(message.role, "value", message.role)).lower()
            text = message_text(message)
            if len(text) > limit:
                body = f"{text[:limit]}…[{len(text)} chars total]"
                messages.append((node_id, role, len(text), body, True))
            else:
                messages.append((node_id, role, len(text), text, False))
        return messages, missing

    async def set_label(self, session_id: str, node_id: int, label: str) -> str:
        """给分支起名（节点 id = 分支起点）；空串表示恢复序号显示。

        :return: 落库后的分支名
        :raises SessionNotFoundError: 会话不存在
        :raises TreeError: 节点不存在
        """
        cleaned = " ".join(label.split())[:LABEL_LIMIT]
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                tree = await self._tree_locked(connection, meta)
                tree.require(node_id)
                await connection.execute(
                    "UPDATE messages SET label = ? WHERE session_id = ? AND id = ?",
                    (cleaned, session_id, node_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return cleaned

    async def delete_node(self, session_id: str, node_id: int, *, now: float) -> int:
        """删除一条分支：该节点与其全部后代一起消失。

        父节点若选中的正是被删分支，则回落到剩下的最后一个孩子（最新分支）。

        :return: 删除的节点数（含后代）
        :raises SessionNotFoundError: 会话不存在
        :raises TreeError: 节点不存在
        """
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                meta = await self._meta_locked(session_id)
                if meta is None:
                    raise SessionNotFoundError(f"会话不存在：{session_id}")
                tree = await self._tree_locked(connection, meta)
                node = tree.require(node_id)
                doomed = tree.subtree(node_id)
                doomed_set = set(doomed)
                placeholders = ",".join("?" * len(doomed))
                await connection.execute(
                    "DELETE FROM messages"
                    f" WHERE session_id = ? AND id IN ({placeholders})",
                    (session_id, *doomed),
                )
                remaining = [
                    child
                    for child in tree.children(node.parent)
                    if child not in doomed_set
                ]
                await self._select_child_locked(
                    connection, session_id, node.parent, pick_current(remaining)
                )
                await connection.execute(
                    """
                    UPDATE sessions
                       SET updated_at = ?,
                           message_count = MAX(message_count - ?, 0)
                     WHERE session_id = ?
                    """,
                    (now, len(doomed), session_id),
                )
                # 分支独有的附件随消息一起回收：message_attachments 有级联删除，
                # 这里把不再被任何消息引用的 blob 一并清掉，不然删了分支图还占着空间
                await self._collect_orphan_attachments_locked(connection)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return len(doomed)

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
                SELECT s.session_id, s.message_count, s.updated_at,
                       s.title, s.preview,
                       (SELECT COUNT(*) FROM session_outbox o
                         WHERE o.session_id = s.session_id) AS queued
                  FROM sessions s
                 ORDER BY s.updated_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            SessionInfo(
                session_id=str(row["session_id"]),
                message_count=int(row["message_count"]),
                updated_at=float(row["updated_at"]),
                title=str(row["title"]),
                preview=str(row["preview"]),
                queued=int(row["queued"]),
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
        # 建表语句全是 IF NOT EXISTS：老库只需补缺失的表与列，不需要重写数据
        await connection.executescript(_SCHEMA)
        if await self._add_label_columns(connection):
            # 老库没有标题/摘要：按历史消息补一份，侧栏立刻就有名字
            await self._backfill_labels(connection)
        if await self._add_outbox_columns(connection):
            # 老队列只有 created_at：按它回填 position，顺序不变
            await connection.execute(
                "UPDATE session_outbox SET position = created_at WHERE position = 0"
            )
        await self._add_message_label_column(connection)
        if await self._add_tree_columns(connection):
            # 老库只有线性顺序：按「前一条是父、后一条是选中分支」补成一条单链
            await self._backfill_linear_tree(connection)
        await connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    async def _add_message_label_column(self, connection: aiosqlite.Connection) -> None:
        """给老库补「分支名」列。

        **不参与** ``_add_tree_columns`` 的返回值判断：那条分支会触发线性历史回填，
        给已经有树结构的老库补列时绝不能重跑回填。
        """
        columns = await _table_columns(connection, "messages")
        if "label" not in columns:
            await connection.execute(
                "ALTER TABLE messages ADD COLUMN label TEXT NOT NULL DEFAULT ''"
            )

    async def _add_outbox_columns(self, connection: aiosqlite.Connection) -> bool:
        """给老库的待发送队列补状态列；返回是否真的补了。"""
        columns = await _table_columns(connection, "session_outbox")
        added = False
        for name, ddl in (
            ("state", "TEXT NOT NULL DEFAULT 'pending'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("error", "TEXT"),
            ("claimed_at", "REAL"),
            ("position", "REAL NOT NULL DEFAULT 0"),
        ):
            if name in columns:
                continue
            await connection.execute(
                f"ALTER TABLE session_outbox ADD COLUMN {name} {ddl}"
            )
            added = True
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_outbox_position"
            " ON session_outbox(session_id, position)"
        )
        return added

    async def _add_label_columns(self, connection: aiosqlite.Connection) -> bool:
        """给老库补 title / preview 列；返回是否真的补了（补了才需要回填）。"""
        session_columns = await _table_columns(connection, "sessions")
        added = False
        if "title" not in session_columns:
            await connection.execute(
                "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
            added = True
        if "preview" not in session_columns:
            await connection.execute(
                "ALTER TABLE sessions ADD COLUMN preview TEXT NOT NULL DEFAULT ''"
            )
            added = True
        return added

    async def _backfill_labels(self, connection: aiosqlite.Connection) -> None:
        """按历史消息回填标题与摘要。

        升级前的会话都是单链（v3 的线性历史在 v4 迁移里已折成一条链），
        所以按 ``id`` 升序取首条 / 末条用户消息，与当前链顺序一致。
        """
        cursor = await connection.execute(
            "SELECT session_id, payload FROM messages ORDER BY session_id, id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        labels: dict[str, tuple[str, str]] = {}
        for row in rows:
            session_id = str(row["session_id"])
            title, preview = labels.get(session_id, ("", ""))
            labels[session_id] = _refresh_labels(str(row["payload"]), title, preview)
        if not labels:
            return
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.executemany(
                "UPDATE sessions SET title = ?, preview = ? WHERE session_id = ?",
                [
                    (title, preview, session_id)
                    for session_id, (title, preview) in labels.items()
                ],
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async def _add_tree_columns(self, connection: aiosqlite.Connection) -> bool:
        """给 v4 之前的老库补上树形列。

        :return: 是否真的补了列（补了说明库里有线性数据要回填指针）
        """
        added = False
        message_columns = await _table_columns(connection, "messages")
        if "parent_id" not in message_columns:
            await connection.execute(
                "ALTER TABLE messages ADD COLUMN parent_id INTEGER NOT NULL DEFAULT 0"
            )
            added = True
        if "current_id" not in message_columns:
            await connection.execute(
                "ALTER TABLE messages ADD COLUMN current_id INTEGER"
            )
            added = True
        session_columns = await _table_columns(connection, "sessions")
        if "root_current_id" not in session_columns:
            await connection.execute(
                "ALTER TABLE sessions ADD COLUMN root_current_id INTEGER"
            )
        # 索引必须在这里建：老库的 messages 表要先补好列才能引用 parent_id，
        # 所以它不能写在 _SCHEMA 里（executescript 会先于补列执行）
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_parent"
            " ON messages(session_id, parent_id)"
        )
        return added

    async def _backfill_linear_tree(self, connection: aiosqlite.Connection) -> None:
        """把线性历史原样折成一条单链：顺序不变，只是补出父子与选中指针。"""
        cursor = await connection.execute(
            "SELECT session_id, id FROM messages ORDER BY session_id, id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        grouped: dict[str, list[int]] = {}
        for row in rows:
            grouped.setdefault(str(row["session_id"]), []).append(int(row["id"]))
        if not grouped:
            return
        updates: list[tuple[int, int | None, int]] = []
        roots: list[tuple[int, str]] = []
        for session_id, ids in grouped.items():
            for index, node_id in enumerate(ids):
                parent = ids[index - 1] if index else ROOT_ID
                current = ids[index + 1] if index + 1 < len(ids) else None
                updates.append((parent, current, node_id))
            roots.append((ids[0], session_id))
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.executemany(
                "UPDATE messages SET parent_id = ?, current_id = ? WHERE id = ?",
                updates,
            )
            await connection.executemany(
                "UPDATE sessions SET root_current_id = ? WHERE session_id = ?",
                roots,
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        logger.info(
            "会话库已升级到 schema %s：%s 个会话 / %s 条消息折成单链",
            _SCHEMA_VERSION,
            len(grouped),
            len(updates),
        )

    async def _record_locked(self, session_id: str) -> SessionRecord | None:
        meta = await self._meta_locked(session_id)
        if meta is None:
            return None
        connection = self._connection_or_raise()
        return SessionRecord(
            meta=meta, nodes=await self._nodes_locked(connection, session_id)
        )

    async def _nodes_locked(
        self, connection: aiosqlite.Connection, session_id: str
    ) -> list[SessionNodeRecord]:
        """按 id 升序读出该会话的全部节点。"""
        cursor = await connection.execute(
            """
            SELECT id, parent_id, current_id, payload, created_at, label
              FROM messages
             WHERE session_id = ?
             ORDER BY id
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            SessionNodeRecord(
                id=int(row["id"]),
                parent=int(row["parent_id"]),
                current=None if row["current_id"] is None else int(row["current_id"]),
                message=str(row["payload"]),
                created_at=float(row["created_at"]),
                label=str(row["label"] or ""),
            )
            for row in rows
        ]

    async def _tree_locked(
        self, connection: aiosqlite.Connection, meta: SessionMeta
    ) -> Tree[SessionNodeRecord]:
        """事务内的树视图（节点 + 根指针）。"""
        nodes = await self._nodes_locked(connection, meta.session_id)
        return Tree(nodes, root_current=meta.root_current)

    async def _resolve_mount_locked(
        self,
        connection: aiosqlite.Connection,
        meta: SessionMeta,
        parent: int | None,
    ) -> int:
        """新消息该挂在谁下面：显式 ``parent`` 优先，否则挂到当前链末端。

        :raises TreeError: 显式给出的挂载点不存在
        """
        if parent is not None and parent != ROOT_ID:
            tree = await self._tree_locked(connection, meta)
            tree.require(parent)
            return parent
        if parent == ROOT_ID:
            return ROOT_ID
        tree = await self._tree_locked(connection, meta)
        return tree.leaf() or ROOT_ID

    async def _select_child_locked(
        self,
        connection: aiosqlite.Connection,
        session_id: str,
        parent: int,
        child: int | None,
    ) -> None:
        """把 ``parent`` 的当前分支指向 ``child``（``None`` 表示取消选中）。"""
        if parent == ROOT_ID:
            await connection.execute(
                "UPDATE sessions SET root_current_id = ? WHERE session_id = ?",
                (child, session_id),
            )
        else:
            await connection.execute(
                "UPDATE messages SET current_id = ? WHERE id = ? AND session_id = ?",
                (child, parent, session_id),
            )

    async def _meta_locked(self, session_id: str) -> SessionMeta | None:
        connection = self._connection_or_raise()
        cursor = await connection.execute(
            """
            SELECT session_id, created_at, updated_at, steps, message_count,
                   root_current_id, title, preview
              FROM sessions
             WHERE session_id = ?
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return SessionMeta(
            session_id=str(row["session_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            steps=int(row["steps"]),
            message_count=int(row["message_count"]),
            root_current=(
                None if row["root_current_id"] is None else int(row["root_current_id"])
            ),
            title=str(row["title"]),
            preview=str(row["preview"]),
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

    async def enqueue_message(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment],
        *,
        now: float,
    ) -> QueuedMessage:
        """把一条消息放进该会话的待发送队列（会话必须已存在）。"""
        message = QueuedMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            text=text,
            attachments=list(attachments),
            created_at=now,
        )
        payload = json.dumps(
            {
                "text": text,
                "attachments": [item.model_dump(mode="json") for item in attachments],
            },
            ensure_ascii=False,
        )
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    INSERT INTO session_outbox
                        (id, session_id, payload, created_at, state, position)
                    VALUES (?, ?, ?, ?, 'pending',
                            (SELECT COALESCE(MAX(position), 0) + 1
                               FROM session_outbox WHERE session_id = ?))
                    """,
                    (message.id, session_id, payload, now, session_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return message

    async def list_outbox(self, session_id: str) -> list[QueuedMessage]:
        """按入队顺序列出待发送消息。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                SELECT id, payload, created_at, state, attempts, error
                  FROM session_outbox
                 WHERE session_id = ?
                 ORDER BY position, rowid
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            _queued_from_row(row, position, session_id)
            for position, row in enumerate(rows, 1)
        ]

    async def remove_message(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息；返回是否确实删掉了。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                "DELETE FROM session_outbox WHERE session_id = ? AND id = ?",
                (session_id, message_id),
            )
            await connection.commit()
            removed = cursor.rowcount > 0
            await cursor.close()
        return removed

    async def claim_message(
        self, session_id: str, *, now: float, stale_after: float
    ) -> QueuedMessage | None:
        """认领队首一条（pending → sending），并回收超时未完成的认领。

        认领是**改状态**而不是删行：agent 崩了也不会丢消息，超过
        ``stale_after`` 秒仍停在 sending 的会自动回到 pending 重新排队。
        """
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    UPDATE session_outbox
                       SET state = 'pending', claimed_at = NULL
                     WHERE session_id = ? AND state = 'sending'
                       AND COALESCE(claimed_at, 0) <= ?
                    """,
                    (session_id, now - stale_after),
                )
                cursor = await connection.execute(
                    """
                    UPDATE session_outbox
                       SET state = 'sending', claimed_at = ?, attempts = attempts + 1
                     WHERE id = (
                        SELECT id FROM session_outbox
                         WHERE session_id = ? AND state = 'pending'
                         ORDER BY position, rowid
                         LIMIT 1
                     )
                    RETURNING id, payload, created_at, state, attempts, error
                    """,
                    (now, session_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        if row is None:
            return None
        return _queued_from_row(row, 1, session_id)

    async def complete_message(self, message_id: str) -> bool:
        """认领的消息跑完了：从队列里删掉。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                "DELETE FROM session_outbox WHERE id = ?", (message_id,)
            )
            await connection.commit()
            done = cursor.rowcount > 0
            await cursor.close()
        return done

    async def fail_message(self, message_id: str, error: str) -> bool:
        """认领的消息失败了：留错误原因，等重试或人工处理。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                UPDATE session_outbox
                   SET state = 'failed', error = ?, claimed_at = NULL
                 WHERE id = ?
                """,
                (error[:500], message_id),
            )
            await connection.commit()
            failed = cursor.rowcount > 0
            await cursor.close()
        return failed

    async def retry_message(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发（重新排队）。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                UPDATE session_outbox
                   SET state = 'pending', error = NULL, claimed_at = NULL
                 WHERE session_id = ? AND id = ? AND state = 'failed'
                """,
                (session_id, message_id),
            )
            await connection.commit()
            queued = cursor.rowcount > 0
            await cursor.close()
        return queued

    async def update_message(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment],
    ) -> bool:
        """改排队消息的正文/附件；正在发送的那条不允许改。"""
        payload = json.dumps(
            {
                "text": text,
                "attachments": [item.model_dump(mode="json") for item in attachments],
            },
            ensure_ascii=False,
        )
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                UPDATE session_outbox
                   SET payload = ?, error = NULL
                 WHERE session_id = ? AND id = ? AND state != 'sending'
                """,
                (payload, session_id, message_id),
            )
            await connection.commit()
            changed = cursor.rowcount > 0
            await cursor.close()
        return changed

    async def move_message(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序：top 置顶 / up 上移一位 / down 下移一位。

        实现方式是「重排后按 1..n 重写 position」——顺序只由 position 决定，
        不用关心浮点间隔，也不会因为并发插入撞号。
        """
        async with self._lock:
            connection = self._connection_or_raise()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT id FROM session_outbox
                     WHERE session_id = ?
                     ORDER BY position, rowid
                    """,
                    (session_id,),
                )
                ids = [str(row["id"]) for row in await cursor.fetchall()]
                await cursor.close()
                if message_id not in ids:
                    await connection.commit()
                    return False
                index = ids.index(message_id)
                ids.pop(index)
                if to == "top":
                    ids.insert(0, message_id)
                elif to == "up":
                    ids.insert(max(0, index - 1), message_id)
                elif to == "down":
                    ids.insert(min(len(ids), index + 1), message_id)
                else:
                    raise ValueError(f"未知的移动目标：{to}")
                await connection.executemany(
                    "UPDATE session_outbox SET position = ? WHERE id = ?",
                    [(position, item) for position, item in enumerate(ids, 1)],
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return True

    async def clear_outbox(self, session_id: str) -> int:
        """清空队列；发送中的那条保留（它正在跑，抽走会让状态更难解释）。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                "DELETE FROM session_outbox"
                " WHERE session_id = ? AND state != 'sending'",
                (session_id,),
            )
            await connection.commit()
            removed = cursor.rowcount
            await cursor.close()
        return int(removed)

    async def outbox_depth(self, session_id: str) -> int:
        """该会话还有几条待发（pending + sending），用于启动时恢复队列泵。"""
        async with self._lock:
            connection = self._connection_or_raise()
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS depth FROM session_outbox
                 WHERE session_id = ? AND state IN ('pending', 'sending')
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["depth"]) if row is not None else 0

    def _connection_or_raise(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("会话数据库尚未连接")
        return self._connection


def _refresh_labels(payload: str, title: str, preview: str) -> tuple[str, str]:
    """按一条消息刷新会话的标题与摘要（只认用户消息，解析失败就原样返回）。"""
    try:
        message = decode_message(payload)
    except ValueError:
        return title, preview
    if message.role is not MessageRole.USER:
        return title, preview
    raw = message_text(message)
    if not raw.strip():
        return title, preview
    return (title or condense(raw), condense(raw, limit=PREVIEW_LIMIT))


async def _table_columns(connection: aiosqlite.Connection, table: str) -> set[str]:
    """读取表的列名（迁移时判断要不要补列）。"""
    cursor = await connection.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return {str(row["name"]) for row in rows}


def _like_escape(text: str) -> str:
    """转义 LIKE 的通配符，让 ``%`` / ``_`` 当普通字符。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, position: int, length: int) -> str:
    """命中处前后各留一点上下文，压平空白成一行。"""
    half = max(20, (SNIPPET_CHARS - length) // 2)
    start = max(0, position - half)
    end = min(len(text), position + length + half)
    piece = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{piece}{suffix}"


def _queued_from_row(
    row: Iterable[object], position: int, session_id: str
) -> QueuedMessage:
    """把一行 outbox 记录还原成 :class:`QueuedMessage`。

    列顺序固定为 ``id, payload, created_at, state, attempts, error``。
    """
    fields = list(row)
    payload = json.loads(str(fields[1]))
    attachments = [
        SessionAttachment.model_validate(item)
        for item in payload.get("attachments", [])
    ]
    state = str(fields[3]) if len(fields) > 3 else "pending"
    attempts = int(str(fields[4])) if len(fields) > 4 else 0
    error = None if len(fields) < 6 or fields[5] is None else str(fields[5])
    return QueuedMessage(
        id=str(fields[0]),
        session_id=session_id,
        text=str(payload.get("text", "")),
        attachments=attachments,
        created_at=float(str(fields[2])),
        position=position,
        state=cast(OutboxState, state),
        attempts=attempts,
        error=error,
    )
