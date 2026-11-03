"""会话层：Agent 中心维护的多会话状态。

会话是**中心化**的：所有会话只存在于 Agent 进程内，工具进程无状态。
每个会话持有独立的 ``asyncio.Lock``，同一会话内的请求串行执行，不同会话
互不阻塞。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from infinity_agent import Message

from blue.protocol import SessionInfo

__all__ = ["Session", "SessionStore"]


@dataclass
class Session:
    """单个会话的运行时状态。"""

    session_id: str
    messages: list[Message]
    created_at: float
    updated_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    steps: int = 0
    """最近一轮消耗的模型步数。"""

    def touch(self, *, now: float | None = None) -> None:
        """刷新最后更新时间。"""
        self.updated_at = time.time() if now is None else now

    def info(self) -> SessionInfo:
        """导出元信息（不含消息正文）。"""
        return SessionInfo(
            session_id=self.session_id,
            message_count=len(self.messages),
            updated_at=self.updated_at,
        )


class SessionStore:
    """会话仓库：创建、查询、重置与过期回收。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        max_sessions: int = 512,
        idle_ttl: float = 7200.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        """构造会话仓库。

        :param system_prompt: 新会话的首条 system 消息内容。
        :param max_sessions: 会话上限，超出时淘汰最久未活动的会话。
        :param idle_ttl: 空闲回收阈值（秒）。
        :param now: 时钟函数，便于测试注入。
        """
        if max_sessions < 1:
            raise ValueError("max_sessions 必须 >= 1")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl 必须 > 0")
        self._system_prompt = system_prompt
        self._max_sessions = max_sessions
        self._idle_ttl = idle_ttl
        self._now = now
        self._sessions: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def sessions(self) -> dict[str, Session]:
        """会话表的只读快照（键为会话 ID）。"""
        return dict(self._sessions)

    def get(self, session_id: str) -> Session:
        """取出会话，不存在则以 system 消息初始化。

        :raises ValueError: 会话 ID 为空。
        """
        if not session_id.strip():
            raise ValueError("session_id 不能为空")
        session = self._sessions.get(session_id)
        if session is None:
            timestamp = self._now()
            session = Session(
                session_id=session_id,
                messages=[Message.system(self._system_prompt)],
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._sessions[session_id] = session
            self._enforce_capacity()
        return session

    def reset(self, session_id: str) -> bool:
        """移除会话（下次访问即重建）；返回是否确实存在。"""
        return self._sessions.pop(session_id, None) is not None

    def snapshot(self) -> list[SessionInfo]:
        """按最后更新时间倒序返回全部会话元信息。"""
        return [
            session.info()
            for session in sorted(
                self._sessions.values(), key=lambda item: item.updated_at, reverse=True
            )
        ]

    def prune(self) -> list[str]:
        """回收空闲超时的会话，返回被移除的会话 ID。"""
        deadline = self._now() - self._idle_ttl
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.updated_at < deadline and not session.lock.locked()
        ]
        for session_id in expired:
            del self._sessions[session_id]
        return expired

    def _enforce_capacity(self) -> None:
        """超出上限时淘汰最久未活动且当前空闲的会话。"""
        overflow = len(self._sessions) - self._max_sessions
        if overflow <= 0:
            return
        candidates = sorted(
            (
                session
                for session in self._sessions.values()
                if not session.lock.locked()
            ),
            key=lambda item: item.updated_at,
        )
        for session in candidates[:overflow]:
            del self._sessions[session.session_id]
