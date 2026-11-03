"""Agent 侧的会话网关：通过总线 RPC 访问独立 session 服务。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from blue_contracts_session import (
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
    SessionSnapshot,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateResponse,
)
from infinity_agent import Message
from infinity_bus import EventBus
from infinity_bus.templates.request import request

from blue.errors import BlueError
from blue.messages import encode_messages

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
    ) -> None:
        """追加消息（可同时提交附件负载）。"""
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
    ) -> None:
        """追加消息；附件与消息在同一次 RPC 里提交，由会话库按哈希去重。"""
        blobs = [blob.model_dump(mode="json") for blob in attachments]
        response = await request(
            bus_proxy,
            SessionAppendEvent.name,
            {"messages": encode_messages(messages), "attachments": blobs},
            SessionAppendReplyEvent.name,
            session_id=session_id,
            timeout=self._timeout,
        )
        if not isinstance(response, SessionAppendResponse):
            raise BlueError("收到非预期的会话追加响应类型")
        if not response.success:
            raise BlueError(response.error_msg or "会话追加失败")

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
