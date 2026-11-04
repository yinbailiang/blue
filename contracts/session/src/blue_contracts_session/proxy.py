"""会话服务的强类型调用代理。

契约包最清楚「哪个请求配哪个应答」，所以代理放在这里：调用方（agent / webui /
工具服务）只说 ``await session.get(sid)``，不用再自己拼事件名、校验应答类型、
判断 ``success``。

每个方法负责三件样板事：

1. 发请求（``session_id`` 走 ``RequestProtocol`` 的上下文）；
2. 把应答收窄成具体类型（否则调用方拿到的是 ``ResponseProtocol`` 基类）；
3. 业务失败（``success=False``）与超时统一抛 :class:`SessionCallError`。

不依赖 blue SDK，只依赖契约包已有的 pydantic 与 infinity_bus，因此任何进程
（服务、CLI、测试）都能直接拿来用。
"""

from __future__ import annotations

from typing import Any, TypeVar

from infinity_bus import EventBus, EventDeclaration
from infinity_bus.templates.request import ResponseProtocol, request

from blue_contracts_session.events import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendResponse,
    SessionAttachmentGetEvent,
    SessionAttachmentReplyEvent,
    SessionAttachmentResponse,
    SessionBranchActivateEvent,
    SessionBranchActivateReplyEvent,
    SessionBranchActivateResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionNodeDeleteEvent,
    SessionNodeDeleteReplyEvent,
    SessionNodeDeleteResponse,
    SessionNodeLabelEvent,
    SessionNodeLabelReplyEvent,
    SessionNodeLabelResponse,
    SessionOutboxClaimEvent,
    SessionOutboxClaimReplyEvent,
    SessionOutboxClaimResponse,
    SessionOutboxClearEvent,
    SessionOutboxClearReplyEvent,
    SessionOutboxClearResponse,
    SessionOutboxCompleteEvent,
    SessionOutboxCompleteReplyEvent,
    SessionOutboxCompleteResponse,
    SessionOutboxEnqueueEvent,
    SessionOutboxEnqueueReplyEvent,
    SessionOutboxEnqueueResponse,
    SessionOutboxFailEvent,
    SessionOutboxFailReplyEvent,
    SessionOutboxFailResponse,
    SessionOutboxListEvent,
    SessionOutboxListReplyEvent,
    SessionOutboxListResponse,
    SessionOutboxMoveEvent,
    SessionOutboxMoveReplyEvent,
    SessionOutboxMoveResponse,
    SessionOutboxRemoveEvent,
    SessionOutboxRemoveReplyEvent,
    SessionOutboxRemoveResponse,
    SessionOutboxRetryEvent,
    SessionOutboxRetryReplyEvent,
    SessionOutboxRetryResponse,
    SessionOutboxSendNowEvent,
    SessionOutboxSendNowReplyEvent,
    SessionOutboxSendNowResponse,
    SessionOutboxUpdateEvent,
    SessionOutboxUpdateReplyEvent,
    SessionOutboxUpdateResponse,
    SessionReadEvent,
    SessionReadReplyEvent,
    SessionReadResponse,
    SessionRenameEvent,
    SessionRenameReplyEvent,
    SessionRenameResponse,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetResponse,
    SessionSearchEvent,
    SessionSearchReplyEvent,
    SessionSearchResponse,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateResponse,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateResponse,
)

__all__ = ["SessionCallError", "SessionProxy"]

ReplyT = TypeVar("ReplyT", bound=ResponseProtocol)
"""应答类型：由调用方从具体 Response 类推出。"""


class SessionCallError(Exception):
    """会话 RPC 失败：超时、应答类型不符，或服务端返回 ``success=False``。

    ``error_msg`` 就是服务端给的说明；调用方通常直接把消息透给用户。
    """


class SessionProxy:
    """会话服务的客户端代理：一个方法 = 一个请求事件。

    :param bus_proxy: 当前进程的总线出口。
    :param timeout: 单次调用超时（秒），可在方法上覆盖。
    """

    def __init__(self, bus_proxy: EventBus.Proxy, *, timeout: float = 10.0) -> None:
        self._bus = bus_proxy
        self._timeout = timeout

    # -- 内部：三件样板事只在这里做一次 --------------------------------

    async def _call(
        self,
        request_event: type[EventDeclaration],
        reply_event: type[EventDeclaration],
        reply_type: type[ReplyT],
        payload: dict[str, Any],
        *,
        session_id: str | None,
        timeout: float | None = None,
    ) -> ReplyT:
        """发请求 → 收窄类型 → 失败转异常。"""
        try:
            response = await request(
                self._bus,
                request_event.name,
                payload,
                reply_event.name,
                session_id=session_id,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except TimeoutError as error:
            raise SessionCallError(f"{request_event.name} 等待响应超时") from error
        if not isinstance(response, reply_type):
            raise SessionCallError(
                f"{request_event.name} 收到非预期应答：{type(response).__name__}"
            )
        if not response.success:
            raise SessionCallError(response.error_msg or f"{request_event.name} 失败")
        return response

    # -- 会话读写 ------------------------------------------------------

    async def get(
        self, session_id: str, system_prompt: str = "", *, timeout: float | None = None
    ) -> SessionGetResponse:
        """取会话快照（不存在则按 ``system_prompt`` 建）。"""
        return await self._call(
            SessionGetEvent,
            SessionGetReplyEvent,
            SessionGetResponse,
            {"system_prompt": system_prompt},
            session_id=session_id,
            timeout=timeout,
        )

    async def append(
        self,
        session_id: str,
        messages: list[str],
        attachments: list[Any] | None = None,
        *,
        parent: int | None = None,
        timeout: float | None = None,
    ) -> SessionAppendResponse:
        """追加消息链；``parent`` 为 None 时挂在当前链末端。"""
        return await self._call(
            SessionAppendEvent,
            SessionAppendReplyEvent,
            SessionAppendResponse,
            {
                "messages": messages,
                "attachments": attachments or [],
                "parent": parent,
            },
            session_id=session_id,
            timeout=timeout,
        )

    async def update(
        self,
        session_id: str,
        steps: int | None = None,
        touch: bool = False,
        *,
        timeout: float | None = None,
    ) -> SessionUpdateResponse:
        """更新步数/时间戳。"""
        return await self._call(
            SessionUpdateEvent,
            SessionUpdateReplyEvent,
            SessionUpdateResponse,
            {"steps": steps, "touch": touch},
            session_id=session_id,
            timeout=timeout,
        )

    async def state(self, *, timeout: float | None = None) -> SessionStateResponse:
        """列出全部会话。"""
        return await self._call(
            SessionStateEvent,
            SessionStateReplyEvent,
            SessionStateResponse,
            {},
            session_id=None,
            timeout=timeout,
        )

    async def reset(self, session_id: str, *, timeout: float | None = None) -> None:
        """删除会话。"""
        await self._call(
            SessionResetEvent,
            SessionResetReplyEvent,
            SessionResetResponse,
            {},
            session_id=session_id,
            timeout=timeout,
        )

    async def rename(
        self, session_id: str, title: str, *, timeout: float | None = None
    ) -> SessionRenameResponse:
        """改名；空标题恢复自动标题。"""
        return await self._call(
            SessionRenameEvent,
            SessionRenameReplyEvent,
            SessionRenameResponse,
            {"title": title},
            session_id=session_id,
            timeout=timeout,
        )

    # -- 对话树 --------------------------------------------------------

    async def activate_branch(
        self, session_id: str, node: int, *, timeout: float | None = None
    ) -> SessionBranchActivateResponse:
        """切换到某条分支（只改指针）。"""
        return await self._call(
            SessionBranchActivateEvent,
            SessionBranchActivateReplyEvent,
            SessionBranchActivateResponse,
            {"node": node},
            session_id=session_id,
            timeout=timeout,
        )

    async def delete_node(
        self, session_id: str, node: int, *, timeout: float | None = None
    ) -> SessionNodeDeleteResponse:
        """删除一条分支（节点与其后代）。"""
        return await self._call(
            SessionNodeDeleteEvent,
            SessionNodeDeleteReplyEvent,
            SessionNodeDeleteResponse,
            {"node": node},
            session_id=session_id,
            timeout=timeout,
        )

    async def label_node(
        self, session_id: str, node: int, label: str, *, timeout: float | None = None
    ) -> SessionNodeLabelResponse:
        """给分支起名；空串恢复序号。"""
        return await self._call(
            SessionNodeLabelEvent,
            SessionNodeLabelReplyEvent,
            SessionNodeLabelResponse,
            {"node": node, "label": label},
            session_id=session_id,
            timeout=timeout,
        )

    # -- 历史回取 ------------------------------------------------------

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 5,
        roles: str = "user,assistant,tool",
        timeout: float | None = None,
    ) -> SessionSearchResponse:
        """在当前链上按子串搜索历史。"""
        return await self._call(
            SessionSearchEvent,
            SessionSearchReplyEvent,
            SessionSearchResponse,
            {"query": query, "limit": limit, "roles": roles},
            session_id=session_id,
            timeout=timeout,
        )

    async def read(
        self,
        session_id: str,
        ids: list[int],
        *,
        max_chars: int = 4000,
        timeout: float | None = None,
    ) -> SessionReadResponse:
        """按 id 读历史原文。"""
        return await self._call(
            SessionReadEvent,
            SessionReadReplyEvent,
            SessionReadResponse,
            {"ids": ids, "max_chars": max_chars},
            session_id=session_id,
            timeout=timeout,
        )

    # -- 附件 ----------------------------------------------------------

    async def attachment(
        self, digest: str, *, timeout: float | None = None
    ) -> SessionAttachmentResponse:
        """按哈希取附件（base64）。"""
        return await self._call(
            SessionAttachmentGetEvent,
            SessionAttachmentReplyEvent,
            SessionAttachmentResponse,
            {"hash": digest},
            session_id=None,
            timeout=timeout,
        )

    # -- 发件箱 --------------------------------------------------------

    async def enqueue(
        self,
        session_id: str,
        text: str,
        attachments: list[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> SessionOutboxEnqueueResponse:
        """入队一条待发送消息。"""
        return await self._call(
            SessionOutboxEnqueueEvent,
            SessionOutboxEnqueueReplyEvent,
            SessionOutboxEnqueueResponse,
            {"text": text, "attachments": attachments or []},
            session_id=session_id,
            timeout=timeout,
        )

    async def outbox(
        self, session_id: str, *, timeout: float | None = None
    ) -> SessionOutboxListResponse:
        """读队列。"""
        return await self._call(
            SessionOutboxListEvent,
            SessionOutboxListReplyEvent,
            SessionOutboxListResponse,
            {},
            session_id=session_id,
            timeout=timeout,
        )

    async def drop_queued(
        self, session_id: str, message_id: str, *, timeout: float | None = None
    ) -> SessionOutboxRemoveResponse:
        """取消一条排队消息。"""
        return await self._call(
            SessionOutboxRemoveEvent,
            SessionOutboxRemoveReplyEvent,
            SessionOutboxRemoveResponse,
            {"message_id": message_id},
            session_id=session_id,
            timeout=timeout,
        )

    async def update_queued(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> SessionOutboxUpdateResponse:
        """改排队消息的正文/附件。"""
        return await self._call(
            SessionOutboxUpdateEvent,
            SessionOutboxUpdateReplyEvent,
            SessionOutboxUpdateResponse,
            {
                "message_id": message_id,
                "text": text,
                "attachments": attachments or [],
            },
            session_id=session_id,
            timeout=timeout,
        )

    async def move_queued(
        self,
        session_id: str,
        message_id: str,
        to: str,
        *,
        timeout: float | None = None,
    ) -> SessionOutboxMoveResponse:
        """调整排队顺序（top / up / down）。"""
        return await self._call(
            SessionOutboxMoveEvent,
            SessionOutboxMoveReplyEvent,
            SessionOutboxMoveResponse,
            {"message_id": message_id, "to": to},
            session_id=session_id,
            timeout=timeout,
        )

    async def send_queued_now(
        self, session_id: str, message_id: str, *, timeout: float | None = None
    ) -> SessionOutboxSendNowResponse:
        """把排队消息置顶并立刻发送。"""
        return await self._call(
            SessionOutboxSendNowEvent,
            SessionOutboxSendNowReplyEvent,
            SessionOutboxSendNowResponse,
            {"message_id": message_id},
            session_id=session_id,
            timeout=timeout,
        )

    async def retry_queued(
        self, session_id: str, message_id: str, *, timeout: float | None = None
    ) -> SessionOutboxRetryResponse:
        """把失败项放回待发。"""
        return await self._call(
            SessionOutboxRetryEvent,
            SessionOutboxRetryReplyEvent,
            SessionOutboxRetryResponse,
            {"message_id": message_id},
            session_id=session_id,
            timeout=timeout,
        )

    async def clear_outbox(
        self, session_id: str, *, timeout: float | None = None
    ) -> SessionOutboxClearResponse:
        """清空队列（正在发送的那条保留）。"""
        return await self._call(
            SessionOutboxClearEvent,
            SessionOutboxClearReplyEvent,
            SessionOutboxClearResponse,
            {},
            session_id=session_id,
            timeout=timeout,
        )

    async def claim_queued(
        self, session_id: str, *, timeout: float | None = None
    ) -> SessionOutboxClaimResponse:
        """认领队首（agent 队列泵用）。"""
        return await self._call(
            SessionOutboxClaimEvent,
            SessionOutboxClaimReplyEvent,
            SessionOutboxClaimResponse,
            {},
            session_id=session_id,
            timeout=timeout,
        )

    async def complete_queued(
        self, message_id: str, *, session_id: str = "", timeout: float | None = None
    ) -> SessionOutboxCompleteResponse:
        """认领的队列项跑完了：删掉这一行。"""
        return await self._call(
            SessionOutboxCompleteEvent,
            SessionOutboxCompleteReplyEvent,
            SessionOutboxCompleteResponse,
            {"message_id": message_id},
            session_id=session_id,
            timeout=timeout,
        )

    async def fail_queued(
        self,
        message_id: str,
        error: str,
        *,
        session_id: str = "",
        timeout: float | None = None,
    ) -> SessionOutboxFailResponse:
        """认领的队列项失败了：留原因等重试。"""
        return await self._call(
            SessionOutboxFailEvent,
            SessionOutboxFailReplyEvent,
            SessionOutboxFailResponse,
            {"message_id": message_id, "error": error},
            session_id=session_id,
            timeout=timeout,
        )
