"""WebUI 的总线网关与展示模型。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from blue_contracts_session import (
    QueuedMessage,
    SessionActivity,
    SessionActivityEvent,
    SessionActivityPayload,
    SessionActivityQueryEvent,
    SessionActivityQueryResponse,
    SessionActivityReplyEvent,
    SessionAttachment,
    SessionAttachmentGetEvent,
    SessionAttachmentReplyEvent,
    SessionAttachmentResponse,
    SessionBranchActivateEvent,
    SessionBranchActivateReplyEvent,
    SessionBranchActivateResponse,
    SessionCancelEvent,
    SessionCancelReplyEvent,
    SessionCancelResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionInfo,
    SessionNodeDeleteEvent,
    SessionNodeDeleteReplyEvent,
    SessionNodeDeleteResponse,
    SessionOutboxClearEvent,
    SessionOutboxClearReplyEvent,
    SessionOutboxClearResponse,
    SessionOutboxEvent,
    SessionOutboxListEvent,
    SessionOutboxListReplyEvent,
    SessionOutboxListResponse,
    SessionOutboxMoveEvent,
    SessionOutboxMoveReplyEvent,
    SessionOutboxMoveResponse,
    SessionOutboxPayload,
    SessionOutboxRemoveEvent,
    SessionOutboxRemoveReplyEvent,
    SessionOutboxRetryEvent,
    SessionOutboxRetryReplyEvent,
    SessionOutboxRetryResponse,
    SessionOutboxSendNowEvent,
    SessionOutboxSendNowReplyEvent,
    SessionOutboxSendNowResponse,
    SessionOutboxUpdateEvent,
    SessionOutboxUpdateReplyEvent,
    SessionOutboxUpdateResponse,
    SessionPromptEvent,
    SessionRenameEvent,
    SessionRenameReplyEvent,
    SessionRenameResponse,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetResponse,
    SessionSnapshot,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateResponse,
    SessionStreamEvent,
    SessionStreamPayload,
    parse_attachment_url,
)
from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
)
from infinity_agent import Message
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import ResponseProtocol, request
from pydantic import BaseModel, Field, ValidationError

from blue.control.model import ActualState
from blue.errors import BlueError
from blue.messages import decode_message
from blue.transport import BusRuntime

__all__ = [
    "ActivityHandler",
    "ActivitySink",
    "BusBackend",
    "MessagePartView",
    "MessageView",
    "OutboxHandler",
    "ServiceView",
    "SessionNodeView",
    "SessionStreamHandler",
    "SessionView",
    "SystemView",
    "ToolCallView",
    "WebBackend",
    "WebUIError",
    "serialize_snapshot",
]


ACTIVITY_QUERY_TIMEOUT = 5.0
"""查询会话运行状态的超时：查不到就当没有，不能拖慢启动。"""


class WebUIError(BlueError):
    """WebUI 后端可向 HTTP/WebSocket 层展示的错误。"""


class StreamSink(Protocol):
    """流式事件汇聚目标。"""

    def dispatch_stream(self, payload: SessionStreamPayload) -> None:
        """把事件分发给该会话的订阅者。"""
        ...


class SessionStreamHandler(EventHandler):
    """把总线上的 Agent 流式事件投递到 WebUI 订阅队列。"""

    def __init__(self, sink: StreamSink) -> None:
        super().__init__(
            subscriptions=[SessionStreamEvent.name],
            handle_timeout=1.0,
        )
        self._sink = sink

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, SessionStreamPayload):
            self._sink.dispatch_stream(payload)


class OutboxSink(Protocol):
    """待发送队列变化的汇聚目标。"""

    def dispatch_outbox(self, payload: SessionOutboxPayload) -> None:
        """把队列变化分发给所有订阅者。"""
        ...


class OutboxHandler(EventHandler):
    """把总线上的 ``session.outbox`` 广播投递到 WebUI 订阅队列。"""

    def __init__(self, sink: OutboxSink) -> None:
        super().__init__(
            subscriptions=[SessionOutboxEvent.name],
            handle_timeout=1.0,
        )
        self._sink = sink

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, SessionOutboxPayload):
            self._sink.dispatch_outbox(payload)


class ActivitySink(Protocol):
    """会话运行状态的汇聚目标。"""

    def dispatch_activity(self, payload: SessionActivityPayload) -> None:
        """把状态变化分发给所有订阅者。"""
        ...


class ActivityHandler(EventHandler):
    """把总线上的会话运行状态投递到 WebUI 订阅队列。"""

    def __init__(self, sink: ActivitySink) -> None:
        super().__init__(
            subscriptions=[SessionActivityEvent.name],
            handle_timeout=1.0,
        )
        self._sink = sink

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del bus_proxy, raw_event
        if isinstance(payload, SessionActivityPayload):
            self._sink.dispatch_activity(payload)


class ToolCallView(BaseModel):
    """前端展示用的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MessagePartView(BaseModel):
    """前端展示用的一个内容块（文本 / 图片 / 文件）。"""

    type: str = Field(description="text / image / file")
    text: str = Field(default="", description="文本内容")
    hash: str = Field(default="", description="附件内容哈希")
    mime: str = Field(default="", description="附件 MIME 类型")
    url: str = Field(default="", description="附件访问地址")
    filename: str | None = Field(default=None, description="文件名（file 块）")


class MessageView(BaseModel):
    """前端展示用的一条消息。"""

    id: int | None = Field(default=None, description="所属树节点 id")
    role: str
    name: str | None = None
    content: str = ""
    parts: list[MessagePartView] = Field(default_factory=list[MessagePartView])
    reasoning_content: str | None = None
    tool_calls: list[ToolCallView] = Field(default_factory=list[ToolCallView])
    tool_call_id: str | None = None


class SessionNodeView(BaseModel):
    """前端展示用的一个树节点：一条消息 + 它在树里的位置。"""

    id: int
    parent: int = Field(description="父节点 id；0 表示会话根")
    current: int | None = Field(default=None, description="当前选中的子节点")
    created_at: float = 0.0
    message: MessageView


class SessionView(BaseModel):
    """会话快照的 JSON 视图。

    ``messages`` 是当前链（界面直接按顺序渲染），``nodes`` 是整棵树：前端用它
    算出某个节点的兄弟分支，从而画出 ``‹ 2/3 ›`` 切换条。
    """

    session_id: str
    created_at: float
    updated_at: float
    steps: int
    message_count: int
    messages: list[MessageView] = Field(default_factory=list[MessageView])
    nodes: list[SessionNodeView] = Field(default_factory=list[SessionNodeView])
    current: list[int] = Field(
        default_factory=list[int], description="当前链的节点 id（早 → 新）"
    )
    root_current: int | None = Field(default=None, description="根选中的顶层分支")


class ServiceView(BaseModel):
    """supervisor 中的一个服务。"""

    name: str
    kind: str
    status: str
    pid: int | None = None
    restarts: int = 0
    package_version: str = ""


class SystemView(BaseModel):
    """系统状态面板数据。"""

    available: bool = False
    supervisor_pid: int | None = None
    generation: str | None = None
    updated_at: float | None = None
    services: list[ServiceView] = Field(default_factory=list[ServiceView])


class WebBackend(Protocol):
    """HTTP/WebSocket 层依赖的后端能力。"""

    async def start(self) -> None:
        """连接后端资源。"""
        ...

    async def stop(self) -> None:
        """释放后端资源。"""
        ...

    async def sessions(self) -> list[SessionInfo]:
        """列出会话。"""
        ...

    async def activity(self) -> list[SessionActivity]:
        """当前正在运行的会话（刷新后恢复「进行中」状态用）。"""
        ...

    def subscribe_activity(
        self,
    ) -> AbstractAsyncContextManager[asyncio.Queue[SessionActivityPayload]]:
        """订阅会话运行状态变化。"""
        ...

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        """读取会话快照。"""
        ...

    async def outbox(self, session_id: str) -> list[QueuedMessage]:
        """读取该会话的待发送队列。"""
        ...

    async def remove_queued(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息。"""
        ...

    async def rename(self, session_id: str, title: str) -> str:
        """给会话改名；空标题表示恢复自动标题。"""
        ...

    async def update_queued(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> bool:
        """改一条排队消息的正文/附件。"""
        ...

    async def move_queued(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序（top/up/down）。"""
        ...

    async def send_queued_now(self, session_id: str, message_id: str) -> bool:
        """把队列项置顶并叫醒 agent，立刻发送。"""
        ...

    async def retry_queued(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发。"""
        ...

    async def clear_queued(self, session_id: str) -> int:
        """清空队列（发送中的保留）。"""
        ...

    def subscribe_outbox(
        self,
    ) -> AbstractAsyncContextManager[asyncio.Queue[SessionOutboxPayload]]:
        """订阅所有会话的待发送队列变化。"""
        ...

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
        *,
        parent: int | None = None,
        regenerate: bool = False,
    ) -> SessionReplyResponse:
        """发送用户消息（可带附件）并等待最终回复。

        ``parent`` 指定新消息挂在哪条消息下（``None`` = 当前链末端）；
        ``regenerate=True`` 表示只重新生成回复、不追加用户消息。
        """
        ...

    async def activate(self, session_id: str, node: int) -> None:
        """切换当前查看的分支。"""
        ...

    async def delete_node(self, session_id: str, node: int) -> int:
        """删除一条分支（节点 + 后代）；返回删除条数。"""
        ...

    async def get_attachment(self, digest: str) -> tuple[str, bytes] | None:
        """按内容哈希取附件；不存在返回 ``None``。"""
        ...

    async def reset(self, session_id: str) -> bool:
        """删除会话历史。"""
        ...

    async def cancel(self, session_id: str) -> bool:
        """中断该会话正在进行的回复；返回是否确有进行中的 turn。"""
        ...

    async def tools(self) -> CatalogQueryResponse:
        """查询工具目录。"""
        ...

    async def system(self) -> SystemView:
        """读取 supervisor 状态。"""
        ...

    def subscribe(
        self, session_id: str
    ) -> AbstractAsyncContextManager[asyncio.Queue[SessionStreamPayload]]:
        """订阅某个会话的实时流式事件。"""
        ...


class BusBackend:
    """通过 RabbitMQ 总线访问 agent、session 与工具目录。"""

    def __init__(
        self,
        runtime: BusRuntime,
        *,
        request_timeout: float = 600.0,
        state_dir: Path | None = None,
    ) -> None:
        self._runtime = runtime
        self._timeout = request_timeout
        self._state_dir = state_dir or Path(".blue")
        self._proxy: EventBus.Proxy | None = None
        self._stream_queues: dict[str, set[asyncio.Queue[SessionStreamPayload]]] = {}
        self._activity: dict[str, SessionActivity] = {}
        self._activity_queues: set[asyncio.Queue[SessionActivityPayload]] = set()
        self._outbox_queues: set[asyncio.Queue[SessionOutboxPayload]] = set()

    async def start(self) -> None:
        """启动总线成员并注册流式事件订阅。"""
        await self._runtime.start()
        self._proxy = self._runtime.proxy("ui.webui")
        try:
            await self._runtime.register(SessionStreamHandler(self))
            await self._runtime.register(ActivityHandler(self))
            await self._runtime.register(OutboxHandler(self))
        except Exception:
            await self._runtime.stop()
            raise
        await self.refresh_activity()

    async def stop(self) -> None:
        """停止总线成员并清空订阅队列。"""
        self._proxy = None
        self._stream_queues.clear()
        self._activity.clear()
        self._activity_queues.clear()
        self._outbox_queues.clear()
        await self._runtime.stop()

    def dispatch_outbox(self, payload: SessionOutboxPayload) -> None:
        """把某个会话的待发送队列变化推给所有打开的 WebSocket。

        队列是「多端共享」的状态：一个端排队，别的端也要立刻看见，
        所以这里不做会话过滤，由前端按 ``session_id`` 分流。
        """
        for queue in tuple(self._outbox_queues):
            queue.put_nowait(payload)

    @asynccontextmanager
    async def subscribe_outbox(
        self,
    ) -> AsyncGenerator[asyncio.Queue[SessionOutboxPayload]]:
        """订阅所有会话的队列变化（WebSocket 生命周期内有效）。"""
        queue: asyncio.Queue[SessionOutboxPayload] = asyncio.Queue()
        self._outbox_queues.add(queue)
        try:
            yield queue
        finally:
            self._outbox_queues.discard(queue)

    async def rename(self, session_id: str, title: str) -> str:
        """给会话改名；空标题表示恢复自动标题。"""
        response = await self._request(
            SessionRenameEvent.name,
            {"title": title},
            SessionRenameReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionRenameResponse):
            raise WebUIError("收到非预期的改名响应")
        if not response.success:
            raise WebUIError(response.error_msg or "改名失败")
        return response.title

    def dispatch_stream(self, payload: SessionStreamPayload) -> None:
        """把流式事件推给该会话当前打开的 WebSocket。"""
        for queue in tuple(self._stream_queues.get(payload.session_id, ())):
            queue.put_nowait(payload)

    def dispatch_activity(self, payload: SessionActivityPayload) -> None:
        """更新运行状态缓存，并推给所有打开的 WebSocket。"""
        activity = payload.activity
        if activity.running:
            self._activity[activity.session_id] = activity
        else:
            self._activity.pop(activity.session_id, None)
        for queue in tuple(self._activity_queues):
            queue.put_nowait(payload)

    async def activity(self) -> list[SessionActivity]:
        """当前正在运行的会话。"""
        return [item for item in self._activity.values() if item.running]

    async def outbox(self, session_id: str) -> list[QueuedMessage]:
        """读取该会话的待发送队列。"""
        response = await self._request(
            SessionOutboxListEvent.name,
            {},
            SessionOutboxListReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if isinstance(response, SessionOutboxListResponse):
            return list(response.messages)
        return []

    async def update_queued(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> bool:
        """改一条排队消息的正文/附件。"""
        response = await self._request(
            SessionOutboxUpdateEvent.name,
            {
                "message_id": message_id,
                "text": text,
                "attachments": [
                    item.model_dump(mode="json") for item in (attachments or [])
                ],
            },
            SessionOutboxUpdateReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionOutboxUpdateResponse):
            raise WebUIError("收到非预期的队列更新响应")
        return response.success

    async def move_queued(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序（top/up/down）。"""
        response = await self._request(
            SessionOutboxMoveEvent.name,
            {"message_id": message_id, "to": to},
            SessionOutboxMoveReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionOutboxMoveResponse):
            raise WebUIError("收到非预期的队列重排响应")
        return response.success

    async def send_queued_now(self, session_id: str, message_id: str) -> bool:
        """把队列项置顶并叫醒 agent，立刻发送。"""
        response = await self._request(
            SessionOutboxSendNowEvent.name,
            {"message_id": message_id},
            SessionOutboxSendNowReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionOutboxSendNowResponse):
            raise WebUIError("收到非预期的立刻发送响应")
        return response.success

    async def retry_queued(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发。"""
        response = await self._request(
            SessionOutboxRetryEvent.name,
            {"message_id": message_id},
            SessionOutboxRetryReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionOutboxRetryResponse):
            raise WebUIError("收到非预期的重试响应")
        return response.success

    async def clear_queued(self, session_id: str) -> int:
        """清空队列（发送中的保留）。"""
        response = await self._request(
            SessionOutboxClearEvent.name,
            {},
            SessionOutboxClearReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        if not isinstance(response, SessionOutboxClearResponse):
            raise WebUIError("收到非预期的清空响应")
        return response.removed

    async def remove_queued(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息。"""
        response = await self._request(
            SessionOutboxRemoveEvent.name,
            {"message_id": message_id},
            SessionOutboxRemoveReplyEvent.name,
            session_id=session_id,
            timeout=10.0,
        )
        return bool(getattr(response, "removed", False))

    async def refresh_activity(self) -> None:
        """向 Agent 拉一次运行状态快照。

        用短超时：Agent 没起/还是旧版本时不能把 WebUI 启动拖住，状态查不到就
        当作「当前没有会话在跑」，后续广播会补上。
        """
        try:
            response = await self._request(
                SessionActivityQueryEvent.name,
                {},
                SessionActivityReplyEvent.name,
                session_id=self._runtime.member_id,
                timeout=ACTIVITY_QUERY_TIMEOUT,
            )
        except BlueError:
            return
        if not isinstance(response, SessionActivityQueryResponse):
            return
        self._activity = {
            item.session_id: item for item in response.running if item.running
        }

    @asynccontextmanager
    async def subscribe_activity(
        self,
    ) -> AsyncGenerator[asyncio.Queue[SessionActivityPayload]]:
        """订阅运行状态变化（当前 WebSocket 生命周期内有效）。"""
        queue: asyncio.Queue[SessionActivityPayload] = asyncio.Queue()
        self._activity_queues.add(queue)
        try:
            yield queue
        finally:
            self._activity_queues.discard(queue)

    @asynccontextmanager
    async def subscribe(
        self, session_id: str
    ) -> AsyncGenerator[asyncio.Queue[SessionStreamPayload]]:
        """创建一个只在当前 WebSocket 生命周期内有效的队列订阅。"""
        queue: asyncio.Queue[SessionStreamPayload] = asyncio.Queue()
        self._stream_queues.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._stream_queues.get(session_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._stream_queues.pop(session_id, None)

    async def sessions(self) -> list[SessionInfo]:
        """读取 session 服务中的会话列表。"""
        response = await self._request(
            SessionStateEvent.name,
            {},
            SessionStateReplyEvent.name,
            session_id=self._runtime.member_id,
        )
        if not isinstance(response, SessionStateResponse):
            raise WebUIError("收到非预期的会话状态响应")
        if not response.success:
            raise WebUIError(response.error_msg or "读取会话状态失败")
        return response.sessions

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        """读取会话；不存在时返回 ``None``，不会隐式创建空会话。"""
        if not any(item.session_id == session_id for item in await self.sessions()):
            return None
        response = await self._request(
            SessionGetEvent.name,
            {"system_prompt": ""},
            SessionGetReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionGetResponse):
            raise WebUIError("收到非预期的会话读取响应")
        if not response.success:
            return None
        return response.session

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
        *,
        parent: int | None = None,
        regenerate: bool = False,
    ) -> SessionReplyResponse:
        """把用户消息（可带附件）交给 agent，并等待最终回复。"""
        response = await self._request(
            SessionPromptEvent.name,
            {
                "text": text,
                "attachments": [
                    attachment.model_dump(mode="json")
                    for attachment in (attachments or [])
                ],
                "parent": parent,
                "regenerate": regenerate,
            },
            SessionReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionReplyResponse):
            raise WebUIError("收到非预期的 Agent 回复")
        # 失败/中断也照原样返回：交给上层用 reply 帧表达（「已中断」不是错误，
        # 之前一路抛异常会让前端多画一个「⚠️ 已中断」气泡，和中断那条内容重复）
        return response

    async def activate(self, session_id: str, node: int) -> None:
        """切换当前查看的分支：只动指针，历史一条都不丢。"""
        response = await self._request(
            SessionBranchActivateEvent.name,
            {"node": node},
            SessionBranchActivateReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionBranchActivateResponse):
            raise WebUIError("收到非预期的分支切换响应")
        if not response.success:
            raise WebUIError(response.error_msg or "分支切换失败")

    async def delete_node(self, session_id: str, node: int) -> int:
        """删除一条分支（节点与其后代）。"""
        response = await self._request(
            SessionNodeDeleteEvent.name,
            {"node": node},
            SessionNodeDeleteReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionNodeDeleteResponse):
            raise WebUIError("收到非预期的分支删除响应")
        if not response.success:
            raise WebUIError(response.error_msg or "分支删除失败")
        return response.removed

    async def get_attachment(self, digest: str) -> tuple[str, bytes] | None:
        """按内容哈希取附件；不存在返回 None。"""
        response = await self._request(
            SessionAttachmentGetEvent.name,
            {"hash": digest},
            SessionAttachmentReplyEvent.name,
            session_id="webui.attachments",
        )
        if not isinstance(response, SessionAttachmentResponse):
            raise WebUIError("收到非预期的附件响应")
        if not response.success:
            return None
        try:
            data = base64.b64decode(response.data, validate=True)
        except (ValueError, TypeError) as error:
            raise WebUIError(f"附件内容不是合法 base64：{error}") from error
        return response.mime, data

    async def reset(self, session_id: str) -> bool:
        """删除会话历史。"""
        response = await self._request(
            SessionResetEvent.name,
            {},
            SessionResetReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionResetResponse):
            raise WebUIError("收到非预期的会话重置响应")
        if not response.success:
            raise WebUIError(response.error_msg or "会话重置失败")
        return True

    async def cancel(self, session_id: str) -> bool:
        """请求中断该会话正在进行的回复。

        中断是协作式的：真正停下发生在 Agent 下一个 ``await`` 点，因此这里返回
        ``True`` 只表示请求已送达并且确有进行中的 turn。
        """
        response = await self._request(
            SessionCancelEvent.name,
            {},
            SessionCancelReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionCancelResponse):
            raise WebUIError("收到非预期的中断响应")
        if not response.success:
            raise WebUIError(response.error_msg or "中断失败")
        return response.cancelled

    async def tools(self) -> CatalogQueryResponse:
        """读取工具目录。"""
        response = await self._request(
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            session_id=self._runtime.member_id,
        )
        if not isinstance(response, CatalogQueryResponse):
            raise WebUIError("收到非预期的工具目录响应")
        if not response.success:
            raise WebUIError(response.error_msg or "读取工具目录失败")
        return response

    async def system(self) -> SystemView:
        """读取 supervisor 的 state.json；不可用时返回空状态。"""
        state_path = self._state_dir / "state.json"
        try:
            raw = state_path.read_text(encoding="utf-8")
            state = ActualState.model_validate_json(raw)
        except OSError, ValueError, ValidationError:
            return SystemView()
        return SystemView(
            available=True,
            supervisor_pid=state.supervisor_pid,
            generation=state.generation,
            updated_at=state.updated_at,
            services=[
                ServiceView(
                    name=service.name,
                    kind=service.kind,
                    status=service.status.value,
                    pid=service.pid,
                    restarts=service.restarts,
                    package_version=service.package_version,
                )
                for service in state.services.values()
            ],
        )

    async def _request(
        self,
        request_event: str,
        payload: dict[str, Any],
        reply_event: str,
        *,
        session_id: str,
        timeout: float | None = None,
    ) -> ResponseProtocol:
        if self._proxy is None:
            raise WebUIError("WebUI 总线尚未启动")
        try:
            return await request(
                self._proxy,
                request_event,
                payload,
                reply_event,
                session_id=session_id,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except TimeoutError as error:
            raise WebUIError("等待后端响应超时") from error


def serialize_message(message: Message, *, node_id: int | None = None) -> MessageView:
    """把 ``Message`` 转为不依赖 Pydantic 特例的展示模型。

    :param node_id: 该消息在会话树里的节点 id；前端靠它发分支操作


    多模态消息额外给出 ``parts``：文本块照原样，附件块给出哈希与访问地址
    （``/api/attachments/<hash>``，内容寻址因而可长缓存）。
    """
    content = message.content
    parts: list[MessagePartView] = []
    texts: list[str] = []
    if isinstance(content, str):
        if content:
            parts.append(MessagePartView(type="text", text=content))
            texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if item.text is not None:
                parts.append(MessagePartView(type="text", text=item.text))
                texts.append(item.text)
                continue
            image = item.image_url
            if image is not None:
                digest = parse_attachment_url(image.url) or ""
                parts.append(
                    MessagePartView(
                        type="image",
                        hash=digest,
                        url=f"/api/attachments/{digest}" if digest else image.url,
                    )
                )
                continue
            file = item.file
            if file is not None:
                source = file.file_data or ""
                digest = parse_attachment_url(source) or ""
                parts.append(
                    MessagePartView(
                        type="file",
                        hash=digest,
                        url=f"/api/attachments/{digest}" if digest else source,
                        filename=file.filename,
                    )
                )
    return MessageView(
        id=node_id,
        role=message.role.value,
        name=message.name,
        content="\n".join(texts),
        parts=parts,
        reasoning_content=message.reasoning_content,
        tool_calls=[
            ToolCallView(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in message.tool_calls or []
        ],
        tool_call_id=message.tool_call_id,
    )


def serialize_snapshot(snapshot: SessionSnapshot) -> SessionView:
    """把会话快照转为前端视图：当前链 + 整棵树。"""
    nodes = [
        SessionNodeView(
            id=node.id,
            parent=node.parent,
            current=node.current,
            created_at=node.created_at,
            message=serialize_message(decode_message(node.message), node_id=node.id),
        )
        for node in snapshot.nodes
    ]
    by_id = {node.id: node for node in nodes}
    return SessionView(
        session_id=snapshot.session_id,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        steps=snapshot.steps,
        message_count=snapshot.message_count,
        # 当前链按 current 的顺序渲染，不能依赖节点 id 升序（换分支后不单调）
        messages=[
            by_id[node_id].message for node_id in snapshot.current if node_id in by_id
        ],
        nodes=nodes,
        current=list(snapshot.current),
        root_current=snapshot.root_current,
    )
