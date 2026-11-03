"""WebUI FastAPI 路由与 WebSocket 协议测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from blue_service_webui.app import create_app
from blue_service_webui.backend import ServiceView, SystemView
from blue_service_webui.config import WebUIConfig
from infinity_agent import Message, ToolDefinition
from starlette.testclient import TestClient

from blue.messages import encode_message
from blue.protocol import (
    CatalogQueryResponse,
    SessionInfo,
    SessionReplyResponse,
    SessionSnapshot,
    SessionStreamPayload,
)


class FakeBackend:
    """不连接总线的测试替身。"""

    def __init__(self) -> None:
        self.reset_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.stream_queue: asyncio.Queue[SessionStreamPayload] | None = None

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def cancel(self, session_id: str) -> bool:
        self.cancel_calls.append(session_id)
        return False

    async def sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id="s1",
                message_count=2,
                updated_at=1_700_000_000.0,
            )
        ]

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        if session_id != "s1":
            return None
        return SessionSnapshot(
            session_id="s1",
            messages=[
                encode_message(Message.system("系统提示")),
                encode_message(Message.user("你好")),
                encode_message(Message.assistant(content="你好，我是 Blue。")),
            ],
            created_at=1_700_000_000.0,
            updated_at=1_700_000_001.0,
            steps=1,
            message_count=3,
        )

    async def prompt(self, session_id: str, text: str) -> SessionReplyResponse:
        if self.stream_queue is not None:
            for payload in (
                SessionStreamPayload(
                    session_id=session_id,
                    kind="step_start",
                    step=1,
                ),
                SessionStreamPayload(
                    session_id=session_id,
                    kind="text",
                    step=1,
                    text="收到：",
                ),
                SessionStreamPayload(
                    session_id=session_id,
                    kind="text",
                    step=1,
                    text=text,
                ),
            ):
                self.stream_queue.put_nowait(payload)
        return SessionReplyResponse(
            session_id=session_id,
            request_id="req-1",
            text=f"收到：{text}",
            steps=1,
        )

    async def reset(self, session_id: str) -> bool:
        self.reset_calls.append(session_id)
        return True

    async def tools(self) -> CatalogQueryResponse:
        return CatalogQueryResponse(
            session_id="s1",
            request_id="req-catalog",
            version=3,
            tools=[ToolDefinition.create("echo", "回显输入")],
        )

    async def system(self) -> SystemView:
        return SystemView(
            available=True,
            supervisor_pid=123,
            generation="gen",
            updated_at=1_700_000_000.0,
            services=[
                ServiceView(
                    name="agent",
                    kind="agent",
                    status="Ready",
                    pid=42,
                )
            ],
        )

    @asynccontextmanager
    async def subscribe(
        self, session_id: str
    ) -> AsyncGenerator[asyncio.Queue[SessionStreamPayload]]:
        del session_id
        queue: asyncio.Queue[SessionStreamPayload] = asyncio.Queue()
        self.stream_queue = queue
        try:
            yield queue
        finally:
            self.stream_queue = None


def _client() -> Any:
    backend = FakeBackend()
    app = create_app(
        backend=backend,
        config=WebUIConfig(poll_interval=0.01),
    )
    client: Any = TestClient(app)
    return client


def test_index_and_health() -> None:
    """前端入口与健康检查可用。"""
    client = _client()
    index = client.get("/")
    assert index.status_code == 200
    assert "Blue Agent Studio" in index.text

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_vendor_assets_are_served() -> None:
    """Markdown / 公式渲染依赖的静态资源随包分发且可访问。"""
    client = _client()
    cases = {
        "/static/vendor/marked/marked.min.js": "javascript",
        "/static/vendor/katex/katex.min.js": "javascript",
        "/static/vendor/katex/katex.min.css": "css",
        "/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2": "font",
    }
    for path, marker in cases.items():
        response = client.get(path)
        assert response.status_code == 200, f"{path} 不可访问"
        assert marker in response.headers["content-type"], path
        assert response.content, f"{path} 内容为空"


def test_index_loads_vendor_before_app() -> None:
    """index.html 先加载 vendor 库再加载 app.js，且引入 KaTeX 样式。"""
    client = _client()
    page = client.get("/").text
    marked = page.index("/static/vendor/marked/marked.min.js")
    katex = page.index("/static/vendor/katex/katex.min.js")
    app = page.index("/static/app.js")
    assert marked < katex < app
    assert "/static/vendor/katex/katex.min.css" in page


def test_rest_endpoints() -> None:
    """会话、工具、系统状态与删除接口。"""
    client = _client()

    sessions = client.get("/api/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "s1"

    detail = client.get("/api/sessions/s1")
    assert detail.status_code == 200
    payload: dict[str, Any] = detail.json()
    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "user",
        "assistant",
    ]
    assert payload["messages"][1]["content"] == "你好"

    assert client.get("/api/sessions/missing").status_code == 404

    tools = client.get("/api/tools")
    assert tools.status_code == 200
    assert tools.json()["tools"][0]["function"]["name"] == "echo"

    system = client.get("/api/system")
    assert system.status_code == 200
    assert system.json()["services"][0]["name"] == "agent"

    deleted = client.delete("/api/sessions/s1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_chat_websocket_streams_reply() -> None:
    """WebSocket 依次推送 ready、状态、回复与最终会话。"""
    client = _client()
    with client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json()["type"] == "ready"
        socket.send_json({"type": "prompt", "session_id": "s1", "text": "ping"})
        frames: list[dict[str, Any]] = []
        while True:
            frame: dict[str, Any] = socket.receive_json()
            frames.append(frame)
            if frame["type"] == "session":
                break
    assert frames[0]["type"] == "status"
    stream_kinds = [
        frame["event"]["kind"] for frame in frames if frame["type"] == "stream"
    ]
    assert stream_kinds == ["step_start", "text", "text"]
    reply = next(frame for frame in frames if frame["type"] == "reply")
    assert reply["text"] == "收到：ping"
    assert frames[-1]["session"]["session_id"] == "s1"


def test_websocket_cancel_interrupts_running_prompt() -> None:
    """流式期间收到 cancel 帧会请求中断，并回推 cancelled。"""

    class BlockingBackend(FakeBackend):
        """prompt 一直挂起到收到 cancel 为止。"""

        def __init__(self) -> None:
            super().__init__()
            self.released = asyncio.Event()
            self.cancelled_sessions: list[str] = []

        async def prompt(self, session_id: str, text: str) -> SessionReplyResponse:
            del text
            await self.released.wait()
            return SessionReplyResponse(
                session_id=session_id,
                request_id="req",
                success=False,
                error_msg="已中断",
            )

        async def cancel(self, session_id: str) -> bool:
            self.cancelled_sessions.append(session_id)
            self.released.set()
            return True

    backend = BlockingBackend()
    app = create_app(backend=backend, config=WebUIConfig(poll_interval=0.01))
    client: Any = TestClient(app)

    with client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json()["type"] == "ready"
        socket.send_json({"type": "prompt", "session_id": "s1", "text": "你好"})
        assert socket.receive_json()["type"] == "status"
        socket.send_json({"type": "cancel", "session_id": "s1"})
        frame: dict[str, Any] = socket.receive_json()

    assert frame["type"] == "cancelled"
    assert backend.cancelled_sessions == ["s1"]
