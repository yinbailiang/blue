"""FastAPI 应用：REST API、WebSocket 聊天与本地静态前端。"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any, Literal, cast

from blue_contracts_session import (
    QueuedMessage,
    SessionActivity,
    SessionAttachment,
    SessionInfo,
    SessionStreamPayload,
)
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from infinity_agent import ToolDefinition
from pydantic import BaseModel, Field, ValidationError, model_validator

from blue.errors import BlueError
from blue_service_webui.backend import (
    SessionView,
    SystemView,
    WebBackend,
    serialize_snapshot,
)
from blue_service_webui.config import WebUIConfig

__all__ = ["ChatRequest", "create_app"]

WEB_DIR = Path(__file__).resolve().parent / "web"

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
"""附件哈希（sha256 十六进制）格式。"""


class ChatRequest(BaseModel):
    """REST / WebSocket 用户消息（可携带内联附件）。

    对话树相关：``parent`` 指定新消息挂在哪条消息下（``None`` = 当前链末端），
    ``regenerate`` 表示只重新生成回复（重试），两者都不传就是普通发言。
    """

    session_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=100_000)
    attachments: list[SessionAttachment] = Field(
        default_factory=list[SessionAttachment], description="内联附件"
    )
    parent: int | None = Field(
        default=None, description="新消息的父节点；None 表示当前链末端"
    )
    regenerate: bool = Field(default=False, description="只生成回复、不追加用户消息")
    type: str = Field(default="prompt", description="帧类型（WebSocket 兼容字段）")

    @model_validator(mode="after")
    def _require_content(self) -> ChatRequest:
        """文本与附件至少要有一个（重试不需要新消息，故豁免）。

        :raises ValueError: 两者都为空且不是重试
        """
        if not self.regenerate and not self.text.strip() and not self.attachments:
            raise ValueError("消息不能为空")
        return self


class SessionSummary(SessionInfo):
    """会话列表项：基础元信息 + 运行状态。

    运行状态来自 Agent（唯一知道「谁在跑」的一方），前端刷新或换设备连入后
    靠它恢复「正在生成」的追踪。
    """

    running: bool = Field(default=False, description="是否有正在进行的 turn")
    phase: str = Field(default="", description="当前阶段：thinking/streaming/tool")
    step: int = Field(default=0, description="当前模型步骤序号")


class RenameRequest(BaseModel):
    """会话改名请求（空标题表示恢复自动标题）。"""

    title: str = Field(default="", max_length=80, description="新标题；空 = 自动")


class QueueText(BaseModel):
    """改排队消息正文。"""

    text: str = Field(default="", max_length=100_000)


class QueueMove(BaseModel):
    """调整排队顺序。"""

    to: Literal["top", "up", "down"] = Field(default="top")


class SessionsResponse(BaseModel):
    """会话列表（含运行状态）。"""

    sessions: list[SessionSummary] = Field(default_factory=list[SessionSummary])


class OutboxResponse(BaseModel):
    """待发送队列。"""

    messages: list[QueuedMessage] = Field(default_factory=list[QueuedMessage])


class ActivityResponse(BaseModel):
    """正在运行的会话快照。"""

    sessions: list[SessionActivity] = Field(default_factory=list[SessionActivity])


class ToolsResponse(BaseModel):
    """工具目录。"""

    version: int = 0
    tools: list[ToolDefinition] = Field(default_factory=list[ToolDefinition])


def create_app(*, backend: WebBackend, config: WebUIConfig) -> FastAPI:
    """构造 FastAPI 应用。

    :param backend: 可注入的总线后端（测试用替身也可以）。
    :param config: WebUI 配置。
    """
    app = FastAPI(
        title=config.title,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """返回单页前端。"""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """轻量健康检查，不访问总线。"""
        return {"status": "ok", "service": "webui", "title": config.title}

    @app.get("/api/sessions", response_model=SessionsResponse)
    async def sessions() -> SessionsResponse:
        """列出会话，并附上「谁正在运行」。"""
        try:
            infos = await backend.sessions()
            running = {item.session_id: item for item in await backend.activity()}
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        summaries: list[SessionSummary] = []
        for info in infos:
            activity = running.get(info.session_id)
            summaries.append(
                SessionSummary(
                    session_id=info.session_id,
                    message_count=info.message_count,
                    updated_at=info.updated_at,
                    title=info.title,
                    preview=info.preview,
                    queued=info.queued,
                    running=bool(activity and activity.running),
                    phase=activity.phase if activity else "",
                    step=activity.step if activity else 0,
                )
            )
        return SessionsResponse(sessions=summaries)

    @app.get("/api/activity", response_model=ActivityResponse)
    async def activity() -> ActivityResponse:
        """当前正在运行的会话（前端首屏/重连时补状态用）。"""
        try:
            return ActivityResponse(sessions=await backend.activity())
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/sessions/{session_id}/outbox", response_model=OutboxResponse)
    async def outbox(session_id: str) -> OutboxResponse:
        """该会话的待发送队列（刷新/换端都能看到）。"""
        try:
            return OutboxResponse(messages=await backend.outbox(session_id))
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.patch(
        "/api/sessions/{session_id}/outbox/{message_id}",
        response_model=OutboxResponse,
    )
    async def edit_queued(
        session_id: str, message_id: str, request: QueueText
    ) -> OutboxResponse:
        """改一条排队消息的正文，返回最新队列。"""
        try:
            await backend.update_queued(session_id, message_id, request.text)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutboxResponse(messages=await backend.outbox(session_id))

    @app.post(
        "/api/sessions/{session_id}/outbox/{message_id}/move",
        response_model=OutboxResponse,
    )
    async def move_queued(
        session_id: str, message_id: str, request: QueueMove
    ) -> OutboxResponse:
        """调整排队顺序（置顶/上移/下移）。"""
        try:
            await backend.move_queued(session_id, message_id, request.to)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutboxResponse(messages=await backend.outbox(session_id))

    @app.post(
        "/api/sessions/{session_id}/outbox/{message_id}/send-now",
        response_model=OutboxResponse,
    )
    async def send_queued_now(session_id: str, message_id: str) -> OutboxResponse:
        """立刻发送：置顶并叫醒 agent。"""
        try:
            await backend.send_queued_now(session_id, message_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutboxResponse(messages=await backend.outbox(session_id))

    @app.post(
        "/api/sessions/{session_id}/outbox/{message_id}/retry",
        response_model=OutboxResponse,
    )
    async def retry_queued(session_id: str, message_id: str) -> OutboxResponse:
        """重试失败项。"""
        try:
            await backend.retry_queued(session_id, message_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutboxResponse(messages=await backend.outbox(session_id))

    @app.delete("/api/sessions/{session_id}/outbox", response_model=OutboxResponse)
    async def clear_outbox(session_id: str) -> OutboxResponse:
        """全部取消（正在发送的那条保留）。"""
        try:
            await backend.clear_queued(session_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutboxResponse(messages=await backend.outbox(session_id))

    @app.delete("/api/sessions/{session_id}/outbox/{message_id}")
    async def drop_queued(session_id: str, message_id: str) -> dict[str, bool]:
        """取消一条排队消息。"""
        try:
            removed = await backend.remove_queued(session_id, message_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"removed": removed}

    @app.get("/api/sessions/{session_id}", response_model=SessionView)
    async def session_detail(session_id: str) -> SessionView:
        """读取一个会话的完整消息。"""
        try:
            snapshot = await backend.snapshot(session_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return serialize_snapshot(snapshot)

    @app.patch("/api/sessions/{session_id}/title")
    async def rename_session(session_id: str, request: RenameRequest) -> dict[str, str]:
        """给会话改名；传空字符串表示恢复自动标题。"""
        try:
            title = await backend.rename(session_id, request.title)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"session_id": session_id, "title": title}

    @app.post("/api/sessions/{session_id}/branches/{node}/activate")
    async def activate_branch(session_id: str, node: int) -> SessionView:
        """切换当前查看的分支，返回切换后的快照。"""
        try:
            await backend.activate(session_id, node)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return await _view(backend, session_id)

    @app.delete("/api/sessions/{session_id}/branches/{node}")
    async def delete_branch(session_id: str, node: int) -> SessionView:
        """删除一条分支（该节点与其所有后代），返回删除后的快照。"""
        try:
            await backend.delete_node(session_id, node)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return await _view(backend, session_id)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        """重置会话。"""
        try:
            await backend.reset(session_id)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"ok": True}

    @app.get("/api/attachments/{digest}", include_in_schema=False)
    async def attachment(digest: str) -> Response:
        """按内容哈希返回附件字节（内容寻址，可长缓存）。"""
        if not _HEX_DIGEST.fullmatch(digest):
            raise HTTPException(status_code=400, detail="附件哈希格式非法")
        try:
            found = await backend.get_attachment(digest)
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if found is None:
            raise HTTPException(status_code=404, detail="附件不存在")
        mime, data = found
        return Response(
            content=data,
            media_type=mime or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/tools", response_model=ToolsResponse)
    async def tools() -> ToolsResponse:
        """返回工具目录。"""
        try:
            catalog = await backend.tools()
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return ToolsResponse(version=catalog.version, tools=catalog.tools)

    @app.get("/api/system", response_model=SystemView)
    async def system() -> SystemView:
        """返回 supervisor 状态。"""
        return await backend.system()

    @app.post("/api/chat")
    async def chat(request: ChatRequest) -> dict[str, Any]:
        """无 WebSocket 时的同步聊天接口。"""
        text = request.text.strip()
        try:
            reply = await backend.prompt(
                request.session_id,
                text,
                request.attachments,
                parent=request.parent,
                regenerate=request.regenerate,
            )
        except BlueError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "success": reply.success,
            "text": reply.text,
            "steps": reply.steps,
        }

    @app.websocket("/ws/chat")
    async def chat_socket(websocket: WebSocket) -> None:
        """双向聊天通道。

        除了「发一轮 prompt」，还负责两件跨连接的事：

        - 连上就推一份**运行状态快照**（``type=activity``），刷新或换设备后
          立刻知道哪个会话正在生成；
        - 支持 ``watch`` / ``unwatch`` 帧，订阅某个会话的实时流而不发起新一轮，
          用于接续正在进行的回复。
        """
        await websocket.accept()
        sender = _Sender(websocket)
        frames: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        watchers: dict[str, asyncio.Task[None]] = {}
        prompt_tasks: set[asyncio.Task[None]] = set()
        await sender.send({"type": "ready", "service": "webui"})
        with contextlib.suppress(BlueError):
            await sender.send(
                {
                    "type": "activity",
                    "sessions": [
                        item.model_dump(mode="json")
                        for item in await backend.activity()
                    ],
                }
            )
        pump = asyncio.create_task(
            _activity_pump(sender, backend), name="webui.activity"
        )
        outbox_pump = asyncio.create_task(
            _outbox_pump(sender, backend), name="webui.outbox"
        )
        reader = asyncio.create_task(
            _read_frames(websocket, frames), name="webui.reader"
        )
        try:
            while True:
                frame = await frames.get()
                if frame is None or reader.done():
                    break
                kind = frame.get("type")
                if kind == "prompt":
                    try:
                        request = ChatRequest.model_validate(frame)
                    except ValidationError as error:
                        await sender.send(
                            {"type": "error", "message": f"消息格式错误：{error}"}
                        )
                        continue
                    # 不再拦截「上一轮还在进行中」：会话忙时 agent 会把消息放进
                    # outbox 并立刻回 queued，前端据此显示待发送队列。
                    task = asyncio.create_task(
                        _stream_prompt(
                            sender,
                            backend,
                            request.session_id,
                            request.text.strip(),
                            request.attachments,
                            parent=request.parent,
                            regenerate=request.regenerate,
                            poll_interval=config.poll_interval,
                        ),
                        name=f"webui.prompt.{request.session_id}",
                    )
                    prompt_tasks.add(task)
                    task.add_done_callback(prompt_tasks.discard)
                elif kind == "cancel":
                    session_id = str(frame.get("session_id") or "")
                    with contextlib.suppress(BlueError):
                        if await backend.cancel(session_id):
                            await sender.send(
                                {"type": "cancelled", "session_id": session_id}
                            )
                elif kind == "watch":
                    session_id = str(frame.get("session_id") or "")
                    if session_id and session_id not in watchers:
                        watchers[session_id] = asyncio.create_task(
                            _watch_stream(sender, backend, session_id),
                            name=f"webui.watch.{session_id}",
                        )
                elif kind == "unwatch":
                    session_id = str(frame.get("session_id") or "")
                    watcher = watchers.pop(session_id, None)
                    if watcher is not None:
                        watcher.cancel()
        except WebSocketDisconnect, RuntimeError, OSError:
            return
        finally:
            pending = [pump, outbox_pump, reader, *watchers.values()]
            pending.extend(prompt_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _view(target: WebBackend, session_id: str) -> SessionView:
        """取会话快照视图；不存在时 404。"""
        snapshot = await target.snapshot(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return serialize_snapshot(snapshot)

    return app


class _Sender:
    """串行化 WebSocket 发送：流、状态、回复来自不同任务，写同一个连接。"""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()

    async def send(self, payload: dict[str, Any]) -> None:
        """发送一帧（内部加锁，避免并发写破坏帧边界）。"""
        async with self._lock:
            await self._websocket.send_json(payload)


async def _read_frames(
    websocket: WebSocket, frames: asyncio.Queue[dict[str, Any] | None]
) -> None:
    """把客户端帧读进队列；断开时投递 ``None`` 作为结束信号。"""
    try:
        while True:
            frame = await websocket.receive_json()
            if isinstance(frame, dict):
                frames.put_nowait(cast(dict[str, Any], frame))
    except WebSocketDisconnect, RuntimeError, OSError:
        return
    finally:
        frames.put_nowait(None)


async def _activity_pump(sender: _Sender, backend: WebBackend) -> None:
    """把会话运行状态的变化实时推给这个连接。"""
    async with backend.subscribe_activity() as queue:
        while True:
            payload = await queue.get()
            await sender.send(
                {
                    "type": "activity",
                    "sessions": [payload.activity.model_dump(mode="json")],
                }
            )


def _stream_frame(session_id: str, payload: SessionStreamPayload) -> dict[str, Any]:
    """流式帧：顶层也带 ``session_id``，前端才能把事件路由到对应会话。"""
    return {
        "type": "stream",
        "session_id": payload.session_id or session_id,
        "event": payload.model_dump(mode="json"),
    }


async def _watch_stream(sender: _Sender, backend: WebBackend, session_id: str) -> None:
    """订阅某个会话的实时流（不发起新一轮 prompt）。

    刷新页面、换一台设备连入时，页面靠它接上正在进行的那一轮回复。
    """
    async with backend.subscribe(session_id) as queue:
        while True:
            payload = await queue.get()
            await sender.send(_stream_frame(session_id, payload))


async def _outbox_pump(sender: _Sender, backend: WebBackend) -> None:
    """把待发送队列的变化实时推给这个连接（替代前端轮询）。"""
    async with backend.subscribe_outbox() as queue:
        while True:
            payload = await queue.get()
            await sender.send(
                {
                    "type": "outbox",
                    "session_id": payload.session_id,
                    "messages": [
                        item.model_dump(mode="json") for item in payload.messages
                    ],
                }
            )


async def _stream_prompt(
    sender: _Sender,
    backend: WebBackend,
    session_id: str,
    text: str,
    attachments: list[SessionAttachment],
    *,
    parent: int | None = None,
    regenerate: bool = False,
    poll_interval: float,
) -> None:
    """执行一轮 prompt，并把 Agent 实时流、最终回复与快照推给浏览器。

    取消由连接主循环处理（``type=cancel`` 帧），这里只管把这一轮跑完。
    """
    await sender.send(
        {
            "type": "status",
            "session_id": session_id,
            "phase": "thinking",
            "label": "Agent 正在思考…",
        }
    )
    async with backend.subscribe(session_id) as stream:
        prompt_task = asyncio.create_task(
            backend.prompt(
                session_id, text, attachments, parent=parent, regenerate=regenerate
            )
        )
        stream_task = asyncio.create_task(stream.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {prompt_task, stream_task},
                    timeout=max(poll_interval * 5.0, 2.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stream_task in done:
                    payload = stream_task.result()
                    await sender.send(_stream_frame(session_id, payload))
                    stream_task = asyncio.create_task(stream.get())
                if prompt_task in done:
                    if stream_task.done() and not stream_task.cancelled():
                        payload = stream_task.result()
                    else:
                        try:
                            payload = await asyncio.wait_for(stream_task, timeout=0.1)
                        except TimeoutError:
                            payload = None
                    if payload is not None:
                        await sender.send(_stream_frame(session_id, payload))
                    while not stream.empty():
                        pending = stream.get_nowait()
                        await sender.send(_stream_frame(session_id, pending))
                    break

            reply = await prompt_task
            if reply.queued:
                # 会话忙：消息已进后端队列，本轮没有真正开始
                await sender.send({"type": "queued_ack", "session_id": session_id})
                return
            await sender.send(
                {
                    "type": "reply",
                    "session_id": session_id,
                    "success": reply.success,
                    "text": reply.text,
                    "steps": reply.steps,
                }
            )
            with contextlib.suppress(BlueError):
                snapshot = await backend.snapshot(session_id)
                if snapshot is not None:
                    await sender.send(
                        {
                            "type": "session",
                            "session": serialize_snapshot(snapshot).model_dump(
                                mode="json"
                            ),
                        }
                    )
        except asyncio.CancelledError:
            raise
        except (BlueError, TimeoutError) as error:
            await sender.send({"type": "error", "message": str(error)})
        finally:
            for task in (stream_task, prompt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream_task, prompt_task, return_exceptions=True)
