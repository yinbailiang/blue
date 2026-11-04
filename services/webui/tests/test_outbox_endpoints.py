"""发件箱接口：读取、改正文、重排、立刻发送、重试、单条取消、全部取消。"""

from __future__ import annotations

from typing import Any

from blue_contracts_session import QueuedMessage
from blue_service_webui.app import create_app
from blue_service_webui.config import WebUIConfig
from starlette.testclient import TestClient
from webui_fake_backend import FakeBackend


def _client() -> tuple[Any, FakeBackend]:
    """构造测试客户端与替身后端，并放两条排队消息。"""
    backend = FakeBackend()
    backend.outbox_items = [
        QueuedMessage(id="q1", session_id="s1", text="第一条", position=1),
        QueuedMessage(id="q2", session_id="s1", text="第二条", position=2),
    ]
    app = create_app(backend=backend, config=WebUIConfig(poll_interval=0.01))
    client: Any = TestClient(app)
    return client, backend


def test_outbox_list() -> None:
    """队列接口返回带状态与位置的队列项。"""
    client, _ = _client()
    payload: dict[str, Any] = client.get("/api/sessions/s1/outbox").json()
    assert [item["text"] for item in payload["messages"]] == ["第一条", "第二条"]
    assert payload["messages"][0]["state"] == "pending"


def test_edit_queued_text() -> None:
    """改排队消息正文，返回最新队列。"""
    client, backend = _client()
    response = client.patch("/api/sessions/s1/outbox/q1", json={"text": "改过了"})
    assert response.status_code == 200
    assert backend.queue_calls[-1] == ("update", "s1", "q1", "改过了")
    assert [item["text"] for item in response.json()["messages"]] == [
        "改过了",
        "第二条",
    ]


def test_move_send_now_retry_and_drop() -> None:
    """重排 / 立刻发送 / 重试 / 单条取消都打到对应方法。"""
    client, backend = _client()
    assert (
        client.post("/api/sessions/s1/outbox/q2/move", json={"to": "top"}).status_code
        == 200
    )
    assert client.post("/api/sessions/s1/outbox/q2/send-now").status_code == 200
    assert client.post("/api/sessions/s1/outbox/q1/retry").status_code == 200
    assert client.delete("/api/sessions/s1/outbox/q1").status_code == 200
    kinds = [call[0] for call in backend.queue_calls]
    assert kinds == ["move", "send_now", "retry", "remove"]


def test_clear_all_keeps_nothing_when_idle() -> None:
    """全部取消：清空待发，返回剩余队列。"""
    client, backend = _client()
    response = client.delete("/api/sessions/s1/outbox")
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert backend.queue_calls[-1][0] == "clear"
