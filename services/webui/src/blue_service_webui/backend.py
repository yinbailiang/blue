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
    SessionCallError,
    SessionCancelEvent,
    SessionCancelReplyEvent,
    SessionCancelResponse,
    SessionInfo,
    SessionOutboxEvent,
    SessionOutboxPayload,
    SessionPromptEvent,
    SessionProxy,
    SessionReplyEvent,
    SessionReplyResponse,
    SessionSnapshot,
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
    label: str = Field(default="", description="分支名；空 = 用序号显示")
    message: MessageView


class SessionView(BaseModel):
    """会话快照的 JSON 视图。

    只下发**整棵树**（每个节点自带一条消息）加「当前链的节点 id 列表」；链上的
    消息由客户端按 ``current`` 从 ``nodes`` 取。早先两边各存一份同样的消息正文，
    长会话切一次会话要多传一倍数据（实测 13 MB 里有一半是重复的）。
    """

    session_id: str
    created_at: float
    updated_at: float
    steps: int
    message_count: int
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

    async def set_label(self, session_id: str, node: int, label: str) -> str:
        """给分支起名；空串恢复序号显示。"""
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

    # -- 会话（统一走契约包里的 SessionProxy）--------------------------

    def _session(self) -> SessionProxy:
        """会话服务代理；总线尚未启动时明确报错。"""
        if self._proxy is None:
            raise WebUIError("WebUI 总线尚未启动")
        return SessionProxy(self._proxy, timeout=self._timeout)

    async def sessions(self) -> list[SessionInfo]:
        """读取 session 服务中的会话列表。"""
        return list((await self._session().state()).sessions)

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        """读取会话；不存在时返回 ``None``，不会隐式创建空会话。"""
        if not any(item.session_id == session_id for item in await self.sessions()):
            return None
        try:
            return (await self._session().get(session_id)).session
        except SessionCallError:
            return None

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
        *,
        parent: int | None = None,
        regenerate: bool = False,
    ) -> SessionReplyResponse:
        """发起一轮对话；失败（含取消）由应答的 ``success`` 表达，不抛异常。

        ``parent`` 决定新用户消息挂在哪里（``None`` = 当前链末端）；``regenerate``
        表示不追加用户消息、直接在 ``parent`` 下生成一条新回复（重试）。
        """
        if self._proxy is None:
            raise WebUIError("WebUI 总线尚未启动")
        payload = {
            "text": text,
            "attachments": [
                item.model_dump(mode="json") for item in (attachments or [])
            ],
            "parent": parent,
            "regenerate": regenerate,
        }
        try:
            response = await request(
                self._proxy,
                SessionPromptEvent.name,
                payload,
                SessionReplyEvent.name,
                session_id=session_id,
                timeout=None,
            )
        except TimeoutError as error:
            raise WebUIError("等待 Agent 响应超时") from error
        if not isinstance(response, SessionReplyResponse):
            raise WebUIError("收到非预期的 Agent 回复")
        return response

    async def activate(self, session_id: str, node: int) -> None:
        """切换分支（只改指针）。"""
        try:
            await self._session().activate_branch(session_id, node)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    async def set_label(self, session_id: str, node: int, label: str) -> str:
        """给分支起名；空串恢复序号。"""
        try:
            return (await self._session().label_node(session_id, node, label)).label
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    async def delete_node(self, session_id: str, node: int) -> int:
        """删除一条分支；返回删除条数。"""
        try:
            return (await self._session().delete_node(session_id, node)).removed
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    async def get_attachment(self, digest: str) -> tuple[str, bytes] | None:
        """按哈希取附件；不存在返回 ``None``。"""
        try:
            reply = await self._session().attachment(digest)
        except SessionCallError:
            return None
        return reply.mime, base64.b64decode(reply.data)

    async def reset(self, session_id: str) -> bool:
        """删除会话。"""
        try:
            await self._session().reset(session_id)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error
        return True

    async def rename(self, session_id: str, title: str) -> str:
        """改名；空标题回到自动标题。"""
        try:
            return (await self._session().rename(session_id, title)).title
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    # -- 发件箱 --------------------------------------------------------

    async def outbox(self, session_id: str) -> list[QueuedMessage]:
        """读取待发送队列。"""
        try:
            return list((await self._session().outbox(session_id)).messages)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    async def update_queued(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> bool:
        """改排队消息的正文/附件。"""
        payload = [item.model_dump(mode="json") for item in (attachments or [])]
        try:
            await self._session().update_queued(session_id, message_id, text, payload)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error
        return True

    async def move_queued(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序。"""
        try:
            await self._session().move_queued(session_id, message_id, to)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error
        return True

    async def send_queued_now(self, session_id: str, message_id: str) -> bool:
        """置顶并立刻发送。"""
        try:
            await self._session().send_queued_now(session_id, message_id)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error
        return True

    async def retry_queued(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发。"""
        try:
            await self._session().retry_queued(session_id, message_id)
        except SessionCallError as error:
            raise WebUIError(str(error)) from error
        return True

    async def clear_queued(self, session_id: str) -> int:
        """清空队列；返回删除条数。"""
        try:
            return (await self._session().clear_outbox(session_id)).removed
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

    async def remove_queued(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息。"""
        try:
            return (await self._session().drop_queued(session_id, message_id)).removed
        except SessionCallError as error:
            raise WebUIError(str(error)) from error

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
            label=node.label,
            message=serialize_message(decode_message(node.message), node_id=node.id),
        )
        for node in snapshot.nodes
    ]
    return SessionView(
        session_id=snapshot.session_id,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        steps=snapshot.steps,
        message_count=snapshot.message_count,
        nodes=nodes,
        current=list(snapshot.current),
        root_current=snapshot.root_current,
    )
