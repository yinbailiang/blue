"""Agent 侧的会话网关：通过总线 RPC 访问独立 session 服务。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from blue_contracts_session import (
    QueuedMessage,
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendResponse,
    SessionAttachmentBlob,
    SessionAttachmentGetEvent,
    SessionAttachmentReplyEvent,
    SessionAttachmentResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionOutboxClaimEvent,
    SessionOutboxClaimReplyEvent,
    SessionOutboxClaimResponse,
    SessionOutboxCompleteEvent,
    SessionOutboxCompleteReplyEvent,
    SessionOutboxFailEvent,
    SessionOutboxFailReplyEvent,
    SessionOutboxRetryEvent,
    SessionOutboxRetryReplyEvent,
    SessionSnapshot,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateResponse,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateResponse,
)
from infinity_agent import Message
from infinity_bus import EventBus
from infinity_bus.templates.request import request

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
    """基于总线 RPC 的会话网关。"""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def claim(
        self, bus_proxy: EventBus.Proxy, session_id: str
    ) -> QueuedMessage | None:
        """认领队首一条待发送消息（pending → sending）。"""
        response = await self._outbox_call(
            bus_proxy,
            SessionOutboxClaimEvent.name,
            {},
            SessionOutboxClaimReplyEvent.name,
            session_id,
        )
        if isinstance(response, SessionOutboxClaimResponse):
            return response.message
        return None

    async def complete(self, bus_proxy: EventBus.Proxy, message_id: str) -> None:
        """认领的消息跑完了：从队列删除。"""
        await self._outbox_call(
            bus_proxy,
            SessionOutboxCompleteEvent.name,
            {"message_id": message_id},
            SessionOutboxCompleteReplyEvent.name,
            None,
        )

    async def fail(
        self, bus_proxy: EventBus.Proxy, message_id: str, error: str
    ) -> None:
        """认领的消息失败了：留错误原因，等自动/人工重试。"""
        await self._outbox_call(
            bus_proxy,
            SessionOutboxFailEvent.name,
            {"message_id": message_id, "error": error[:500]},
            SessionOutboxFailReplyEvent.name,
            None,
        )

    async def retry(
        self, bus_proxy: EventBus.Proxy, session_id: str, message_id: str
    ) -> None:
        """把失败项放回待发。"""
        await self._outbox_call(
            bus_proxy,
            SessionOutboxRetryEvent.name,
            {"message_id": message_id},
            SessionOutboxRetryReplyEvent.name,
            session_id,
        )

    async def queued_sessions(self, bus_proxy: EventBus.Proxy) -> list[str]:
        """哪些会话还有待发消息（启动时恢复队列泵）。"""
        try:
            reply = await request(
                bus_proxy,
                SessionStateEvent.name,
                {},
                SessionStateReplyEvent.name,
                session_id="agent.outbox",
                timeout=5.0,
            )
        except Exception:
            logger.warning("读取会话状态失败（恢复队列泵）", exc_info=True)
            return []
        if isinstance(reply, SessionStateResponse):
            return [info.session_id for info in reply.sessions if info.queued > 0]
        return []

    async def _outbox_call(
        self,
        bus_proxy: EventBus.Proxy,
        request_event: str,
        payload: dict[str, object],
        reply_event: str,
        session_id: str | None,
    ) -> object:
        """发一个队列操作请求；失败只记日志，不打断调用方。"""
        try:
            return await request(
                bus_proxy,
                request_event,
                payload,
                reply_event,
                session_id=session_id or "agent.outbox",
                timeout=10.0,
            )
        except Exception:
            logger.warning("队列操作 %s 失败", request_event, exc_info=True)
            return None

    async def ensure(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        system_prompt: str,
    ) -> SessionSnapshot:
        """取/建会话并返回快照。"""
        response = await request(
            bus_proxy,
            SessionGetEvent.name,
            {"system_prompt": system_prompt},
            SessionGetReplyEvent.name,
            session_id=session_id,
            timeout=self._timeout,
        )
        if not isinstance(response, SessionGetResponse):
            raise BlueError("收到非预期的会话获取响应类型")
        if not response.success:
            raise BlueError(response.error_msg or "会话获取失败")
        if response.session is None:
            raise BlueError("会话服务返回了空快照")
        return response.session

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> list[int]:
        """追加消息；附件与消息在同一次 RPC 里提交，由会话库按哈希去重。"""
        blobs = [blob.model_dump(mode="json") for blob in attachments]
        response = await request(
            bus_proxy,
            SessionAppendEvent.name,
            {
                "messages": encode_messages(messages),
                "attachments": blobs,
                "parent": parent,
            },
            SessionAppendReplyEvent.name,
            session_id=session_id,
            timeout=self._timeout,
        )
        if not isinstance(response, SessionAppendResponse):
            raise BlueError("收到非预期的会话追加响应类型")
        if not response.success:
            raise BlueError(response.error_msg or "会话追加失败")
        return list(response.nodes)

    async def get_attachment(
        self, bus_proxy: EventBus.Proxy, digest: str
    ) -> AttachmentPayload | None:
        """按内容哈希取附件；服务端回失败时视为不存在。"""
        response = await request(
            bus_proxy,
            SessionAttachmentGetEvent.name,
            {"hash": digest},
            SessionAttachmentReplyEvent.name,
            timeout=self._timeout,
        )
        if not isinstance(response, SessionAttachmentResponse):
            raise BlueError("收到非预期的附件响应类型")
        if not response.success:
            return None
        return AttachmentPayload(
            hash=response.hash,
            mime=response.mime,
            size=response.size,
            data=response.data,
        )

    async def update(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        steps: int,
    ) -> None:
        """更新会话步数。"""
        response = await request(
            bus_proxy,
            SessionUpdateEvent.name,
            {"steps": steps, "touch": True},
            SessionUpdateReplyEvent.name,
            session_id=session_id,
            timeout=self._timeout,
        )
        if not isinstance(response, SessionUpdateResponse):
            raise BlueError("收到非预期的会话更新响应类型")
        if not response.success:
            raise BlueError(response.error_msg or "会话更新失败")
