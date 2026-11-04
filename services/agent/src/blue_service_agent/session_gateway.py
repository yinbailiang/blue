"""Agent 侧的会话网关：通过总线 RPC 访问独立 session 服务。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from blue_contracts_session import (
    QueuedMessage,
    SessionAttachmentBlob,
    SessionCallError,
    SessionProxy,
    SessionSnapshot,
)
from infinity_agent import Message
from infinity_bus import EventBus

from blue.errors import BlueError
from blue.messages import encode_messages

logger = logging.getLogger(__name__)

__all__ = ["AttachmentPayload", "BusSessionGateway", "SessionGateway"]


@dataclass(frozen=True)
class AttachmentPayload:
    """从会话库取回的附件。"""

    hash: str
    mime: str
    size: int
    data: str
    """裸 base64 负载。"""

    def data_url(self) -> str:
        """拼成模型可接受的 data URL。"""
        return f"data:{self.mime};base64,{self.data}"


class SessionGateway(Protocol):
    """Agent 依赖的会话能力。"""

    async def claim(
        self, bus_proxy: EventBus.Proxy, session_id: str
    ) -> QueuedMessage | None:
        """认领队首一条待发送消息（pending → sending）。"""
        ...

    async def complete(self, bus_proxy: EventBus.Proxy, message_id: str) -> None:
        """认领的消息跑完了。"""
        ...

    async def fail(
        self, bus_proxy: EventBus.Proxy, message_id: str, error: str
    ) -> None:
        """认领的消息失败了。"""
        ...

    async def retry(
        self, bus_proxy: EventBus.Proxy, session_id: str, message_id: str
    ) -> None:
        """把失败项放回待发。"""
        ...

    async def queued_sessions(self, bus_proxy: EventBus.Proxy) -> list[str]:
        """哪些会话还有待发消息（agent 启动时恢复队列泵）。"""
        ...

    async def ensure(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        system_prompt: str,
    ) -> SessionSnapshot:
        """取/建会话。"""
        ...

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> list[int]:
        """追加消息（可同时提交附件负载）。

        :return: 新节点的 id，按插入顺序；末尾那个就是新的链尾
        """
        ...

    async def get_attachment(
        self, bus_proxy: EventBus.Proxy, digest: str
    ) -> AttachmentPayload | None:
        """按内容哈希取附件；不存在返回 ``None``。"""
        ...

    async def update(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        steps: int,
    ) -> None:
        """更新步数。"""
        ...


class BusSessionGateway:
    """基于总线 RPC 的会话网关。

    「发请求 → 校验应答类型 → 判 success」这些样板全在契约包的
    :class:`~blue_contracts_session.SessionProxy` 里；这里只表达 **agent 的语义**：

    - 读路径出错（取快照、追加、更新）→ 抛 :class:`BlueError`，让这一轮失败得明确；
    - 队列操作出错（认领/完成/失败/重试）→ 只记日志，绝不打断正在跑的回合；
    - 附件不存在 → 返回 ``None``（agent 会把该图当"丢失"处理）。
    """

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def _session(self, bus_proxy: EventBus.Proxy) -> SessionProxy:
        """每次调用构造一个轻量代理（它是无状态的，只是带上出口与超时）。"""
        return SessionProxy(bus_proxy, timeout=self._timeout)

    # -- 发件箱 --------------------------------------------------------

    async def claim(
        self, bus_proxy: EventBus.Proxy, session_id: str
    ) -> QueuedMessage | None:
        """认领队首一条待发送消息（pending → sending）。"""
        try:
            reply = await self._session(bus_proxy).claim_queued(session_id)
        except Exception:
            logger.warning("会话 %s 认领队列失败", session_id, exc_info=True)
            return None
        return reply.message

    async def complete(self, bus_proxy: EventBus.Proxy, message_id: str) -> None:
        """认领的消息跑完了：从队列删除。"""
        await self._outbox(bus_proxy, "complete_queued", message_id)

    async def fail(
        self, bus_proxy: EventBus.Proxy, message_id: str, error: str
    ) -> None:
        """认领的消息失败了：留错误原因，等自动/人工重试。"""
        await self._outbox(bus_proxy, "fail_queued", message_id, error[:500])

    async def retry(
        self, bus_proxy: EventBus.Proxy, session_id: str, message_id: str
    ) -> None:
        """把失败项放回待发。"""
        await self._outbox(bus_proxy, "retry_queued", session_id, message_id)

    async def queued_sessions(self, bus_proxy: EventBus.Proxy) -> list[str]:
        """哪些会话还有待发消息（启动时恢复队列泵）。"""
        try:
            reply = await self._session(bus_proxy).state()
        except Exception:
            logger.warning("读取会话状态失败（恢复队列泵）", exc_info=True)
            return []
        return [info.session_id for info in reply.sessions if info.queued > 0]

    async def _outbox(
        self, bus_proxy: EventBus.Proxy, action: str, *args: object
    ) -> None:
        """调一个队列操作；失败只记日志。"""
        method = getattr(self._session(bus_proxy), action)
        try:
            await method(*args)
        except Exception:
            logger.warning("队列操作 %s 失败", action, exc_info=True)

    # -- 会话读写 ------------------------------------------------------

    async def ensure(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        system_prompt: str,
    ) -> SessionSnapshot:
        """取/建会话并返回快照。"""
        try:
            reply = await self._session(bus_proxy).get(session_id, system_prompt)
        except SessionCallError as error:
            raise BlueError(str(error) or "会话获取失败") from error
        if reply.session is None:
            raise BlueError("会话服务返回了空快照")
        return reply.session

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> list[int]:
        """追加消息；附件与消息在同一次 RPC 里提交，由会话库按哈希去重。

        :return: 新节点的 id，按插入顺序；末尾那个就是新的链尾
        """
        blobs = [blob.model_dump(mode="json") for blob in attachments]
        try:
            reply = await self._session(bus_proxy).append(
                session_id, encode_messages(messages), blobs, parent=parent
            )
        except SessionCallError as error:
            raise BlueError(str(error) or "会话追加失败") from error
        return list(reply.nodes)

    async def get_attachment(
        self, bus_proxy: EventBus.Proxy, digest: str
    ) -> AttachmentPayload | None:
        """按内容哈希取附件；不存在返回 ``None``。"""
        try:
            reply = await self._session(bus_proxy).attachment(digest)
        except SessionCallError as error:
            if "不存在" in str(error):
                return None
            raise BlueError(str(error) or "附件读取失败") from error
        return AttachmentPayload(
            hash=reply.hash, mime=reply.mime, size=reply.size, data=reply.data
        )

    async def update(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        steps: int,
    ) -> None:
        """更新会话步数。"""
        try:
            await self._session(bus_proxy).update(session_id, steps, touch=True)
        except SessionCallError as error:
            raise BlueError(str(error) or "会话更新失败") from error
