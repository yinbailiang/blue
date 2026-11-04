"""WebUI 会话元信息：标题/摘要/排队数、改名、帧的会话标识与队列推送。"""

from __future__ import annotations

from typing import Any

from blue_contracts_session import QueuedMessage
from blue_service_webui.app import create_app
from blue_service_webui.config import WebUIConfig
from starlette.testclient import TestClient
from webui_fake_backend import FakeBackend


def _client() -> tuple[Any, FakeBackend]:
    """构造测试客户端与替身后端。"""
    backend = FakeBackend()
    app = create_app(backend=backend, config=WebUIConfig(poll_interval=0.01))
    client: Any = TestClient(app)
    return client, backend


def test_session_list_carries_title_preview_and_queue() -> None:
    """会话列表直接带标题 / 摘要 / 排队条数，前端不再自己攒标题。"""
    client, backend = _client()
    backend.outbox_items = [
        QueuedMessage(id="q1", session_id="s1", text="排队一"),
        QueuedMessage(id="q2", session_id="s1", text="排队二"),
    ]
    payload: dict[str, Any] = client.get("/api/sessions").json()
    item = payload["sessions"][0]
    assert item["title"] == "自动标题"
    assert item["preview"] == "最后一条用户消息"
    assert item["queued"] == 2


def test_rename_endpoint_updates_title() -> None:
    """改名接口把新标题写回后端，并返回落库后的值。"""
    client, backend = _client()
    response = client.patch("/api/sessions/s1/title", json={"title": "  我的会话  "})
    assert response.status_code == 200
    assert response.json()["title"] == "我的会话"
    assert backend.rename_calls == [("s1", "  我的会话  ")]
    assert client.get("/api/sessions").json()["sessions"][0]["title"] == "我的会话"


def test_rename_with_blank_title_restores_auto() -> None:
    """传空标题 = 恢复自动标题。"""
    client, backend = _client()
    response = client.patch("/api/sessions/s1/title", json={"title": ""})
    assert response.status_code == 200
    assert response.json()["title"] == "自动标题"
    assert backend.rename_calls == [("s1", "")]


def test_stream_frames_carry_session_id() -> None:
    """流式帧顶层带 session_id：前端才能把事件路由到对应会话。"""
    client, _ = _client()
    with client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_json()["type"] == "activity"
        socket.send_json({"type": "prompt", "session_id": "s1", "text": "你好"})
        frames: list[dict[str, Any]] = []
        while True:
            frame: dict[str, Any] = socket.receive_json()
            frames.append(frame)
            if frame["type"] == "session":
                break

    kinds = {frame["type"] for frame in frames}
    assert {"status", "stream", "reply", "session"} <= kinds
    for frame in frames:
        if frame["type"] in {"status", "stream", "reply"}:
            assert frame["session_id"] == "s1", frame


def test_outbox_changes_are_pushed_over_the_socket() -> None:
    """队列变化经 session.outbox 广播直接推给浏览器（不再轮询）。"""
    client, backend = _client()
    with client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_json()["type"] == "activity"
        backend.outbox_items = [
            QueuedMessage(id="q1", session_id="s1", text="排队一", position=1)
        ]
        backend.push_outbox()
        frame: dict[str, Any] = socket.receive_json()

    assert frame["type"] == "outbox"
    assert frame["session_id"] == "s1"
    assert [item["text"] for item in frame["messages"]] == ["排队一"]
