"""WebUI 的总线网关与展示模型。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from blue_contracts_session import (
    SessionAttachment,
    SessionAttachmentGetEvent,
    SessionAttachmentReplyEvent,
    SessionAttachmentResponse,
    SessionCancelEvent,
    SessionCancelReplyEvent,
    SessionCancelResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionInfo,
    SessionPromptEvent,
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
from blue.transport import BusRuntime

__all__ = [
    "BusBackend",
    "MessagePartView",
    "MessageView",
    "ServiceView",
    "SessionStreamHandler",
    "SessionView",
    "SystemView",
    "ToolCallView",
    "WebBackend",
    "WebUIError",
    "serialize_snapshot",
]


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

    role: str
    name: str | None = None
    content: str = ""
    parts: list[MessagePartView] = Field(default_factory=list[MessagePartView])
    reasoning_content: str | None = None
    tool_calls: list[ToolCallView] = Field(default_factory=list[ToolCallView])
    tool_call_id: str | None = None


class SessionView(BaseModel):
    """会话快照的 JSON 视图。"""

    session_id: str
    created_at: float
    updated_at: float
    steps: int
    message_count: int
    messages: list[MessageView] = Field(default_factory=list[MessageView])


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

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        """读取会话快照。"""
        ...

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> SessionReplyResponse:
        """发送用户消息（可带附件）并等待最终回复。"""
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

    async def start(self) -> None:
        """启动总线成员并注册流式事件订阅。"""
        await self._runtime.start()
        self._proxy = self._runtime.proxy("ui.webui")
        try:
            await self._runtime.register(SessionStreamHandler(self))
        except Exception:
            await self._runtime.stop()
            raise

    async def stop(self) -> None:
        """停止总线成员并清空订阅队列。"""
        self._proxy = None
        self._stream_queues.clear()
        await self._runtime.stop()

    def dispatch_stream(self, payload: SessionStreamPayload) -> None:
        """把流式事件推给该会话当前打开的 WebSocket。"""
        for queue in tuple(self._stream_queues.get(payload.session_id, ())):
            queue.put_nowait(payload)

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
            },
            SessionReplyEvent.name,
            session_id=session_id,
        )
        if not isinstance(response, SessionReplyResponse):
            raise WebUIError("收到非预期的 Agent 回复")
        if not response.success:
            raise WebUIError(response.error_msg or "Agent 处理失败")
        return response

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
                timeout=self._timeout,
            )
        except TimeoutError as error:
            raise WebUIError("等待后端响应超时") from error


def serialize_message(message: Message) -> MessageView:
    """把 ``Message`` 转为不依赖 Pydantic 特例的展示模型。

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
    """把会话快照转为前端视图。"""
    from blue.messages import decode_messages

    return SessionView(
        session_id=snapshot.session_id,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        steps=snapshot.steps,
        message_count=snapshot.message_count,
        messages=[
            serialize_message(message) for message in decode_messages(snapshot.messages)
        ],
    )
