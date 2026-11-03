"""FastAPI 应用：REST API、WebSocket 聊天与本地静态前端。"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any, cast

from blue_contracts_session import SessionAttachment, SessionInfo
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
    """REST / WebSocket 用户消息（可携带内联附件）。"""

    session_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=100_000)
    attachments: list[SessionAttachment] = Field(
        default_factory=list[SessionAttachment], description="内联附件"
    )
    type: str = Field(default="prompt", description="帧类型（WebSocket 兼容字段）")

    @model_validator(mode="after")
    def _require_content(self) -> ChatRequest:
        """文本与附件至少要有一个。

        :raises ValueError: 两者都为空
        """
        if not self.text.strip() and not self.attachments:
            raise ValueError("消息不能为空")
        return self


class SessionsResponse(BaseModel):
    """会话列表。"""

    sessions: list[SessionInfo] = Field(default_factory=list[SessionInfo])


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
        """列出会话。"""
        try:
            return SessionsResponse(sessions=await backend.sessions())
        except BlueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

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
            reply = await backend.prompt(request.session_id, text, request.attachments)
        except BlueError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "success": reply.success,
            "text": reply.text,
            "steps": reply.steps,
        }

    @app.websocket("/ws/chat")
    async def chat_socket(websocket: WebSocket) -> None:
        """双向聊天通道：先等待回复，期间轮询并推送会话进度。"""
        await websocket.accept()
        await websocket.send_json({"type": "ready", "service": "webui"})
        try:
            while True:
                raw = await websocket.receive_json()
                try:
                    request = ChatRequest.model_validate(raw)
                except ValidationError as error:
                    await websocket.send_json(
                        {"type": "error", "message": f"消息格式错误：{error}"}
                    )
                    continue
                await _stream_prompt(
                    websocket,
                    backend,
                    request.session_id,
                    request.text.strip(),
                    request.attachments,
                    poll_interval=config.poll_interval,
                )
        except WebSocketDisconnect:
            return
        except RuntimeError, OSError:
            return

    return app


async def _stream_prompt(
    websocket: WebSocket,
    backend: WebBackend,
    session_id: str,
    text: str,
    attachments: list[SessionAttachment],
    *,
    poll_interval: float,
) -> None:
    """执行一轮 prompt，并把 Agent 实时流、最终回复与快照推给浏览器。

    期间并发监听客户端帧：收到 ``{"type": "cancel"}`` 就向 Agent 请求中断，
    完成后再补一帧 ``cancelled`` 与最新快照。
    """
    await websocket.send_json(
        {"type": "status", "phase": "thinking", "label": "Agent 正在思考…"}
    )
    async with backend.subscribe(session_id) as stream:
        prompt_task = asyncio.create_task(backend.prompt(session_id, text, attachments))
        stream_task = asyncio.create_task(stream.get())
        receive_task = asyncio.create_task(websocket.receive_json())
        cancelled = False
        try:
            while True:
                done, _ = await asyncio.wait(
                    {prompt_task, stream_task, receive_task},
                    timeout=max(poll_interval * 5.0, 2.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    frame: object = receive_task.result()
                    receive_task = asyncio.create_task(websocket.receive_json())
                    if (
                        isinstance(frame, dict)
                        and cast(dict[str, object], frame).get("type") == "cancel"
                    ):
                        with contextlib.suppress(BlueError):
                            cancelled = await backend.cancel(session_id)
                if stream_task in done:
                    payload = stream_task.result()
                    await websocket.send_json(
                        {
                            "type": "stream",
                            "event": payload.model_dump(mode="json"),
                        }
                    )
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
                        await websocket.send_json(
                            {
                                "type": "stream",
                                "event": payload.model_dump(mode="json"),
                            }
                        )
                    while not stream.empty():
                        pending = stream.get_nowait()
                        await websocket.send_json(
                            {
                                "type": "stream",
                                "event": pending.model_dump(mode="json"),
                            }
                        )
                    break

            if cancelled:
                await websocket.send_json({"type": "cancelled"})
            else:
                reply = await prompt_task
                await websocket.send_json(
                    {
                        "type": "reply",
                        "success": reply.success,
                        "text": reply.text,
                        "steps": reply.steps,
                    }
                )
            with contextlib.suppress(BlueError):
                snapshot = await backend.snapshot(session_id)
                if snapshot is not None:
                    await websocket.send_json(
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
            await websocket.send_json({"type": "error", "message": str(error)})
        finally:
            for task in (receive_task, stream_task, prompt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                receive_task, stream_task, prompt_task, return_exceptions=True
            )
