"""WebUI 对话树接口测试：树快照、分支切换/删除、prompt 的分支参数。"""

from __future__ import annotations

from typing import Any

from blue_service_webui.app import create_app
from blue_service_webui.config import WebUIConfig
from starlette.testclient import TestClient
from webui_fake_backend import FakeBackend


def _client() -> tuple[Any, FakeBackend]:
    """构造测试客户端与替身后端（与 test_app.py 同一套假数据）。"""
    backend = FakeBackend()
    app = create_app(backend=backend, config=WebUIConfig(poll_interval=0.01))
    client: Any = TestClient(app)
    return client, backend


def test_session_view_exposes_the_whole_tree() -> None:
    """会话视图同时给出当前链与整棵树，供前端画分支切换条。"""
    client, _ = _client()
    payload: dict[str, Any] = client.get("/api/sessions/s1").json()

    assert payload["current"] == [1, 2, 3]
    assert [node["id"] for node in payload["nodes"]] == [1, 2, 3, 4]
    assert [node["parent"] for node in payload["nodes"]] == [0, 1, 2, 2]
    # 用户消息下挂着两条回答分支，只有一条在当前链上
    assert payload["nodes"][1]["current"] == 3
    assert [message["content"] for message in payload["messages"]] == [
        "系统提示",
        "你好",
        "你好，我是 Blue。",
    ]
    # 每条消息都带节点 id，前端靠它发分支操作
    assert [message["id"] for message in payload["messages"]] == [1, 2, 3]


def test_activate_endpoint_switches_the_current_chain() -> None:
    """切换分支后返回的快照换成另一条链，节点一条不少。"""
    client, backend = _client()
    response = client.post("/api/sessions/s1/branches/4/activate")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert payload["current"] == [1, 2, 4]
    assert payload["messages"][-1]["content"] == "另一个分支"
    assert len(payload["nodes"]) == 4
    assert backend.activation_calls == [("s1", 4)]


def test_activate_unknown_session_is_404() -> None:
    """不存在的会话切分支返回 404，而不是 500。"""
    client, _ = _client()
    assert client.post("/api/sessions/missing/branches/4/activate").status_code == 404


def test_delete_endpoint_removes_the_branch() -> None:
    """删除分支后，该节点从树与当前链里消失。"""
    client, backend = _client()
    response = client.delete("/api/sessions/s1/branches/4")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert [node["id"] for node in payload["nodes"]] == [1, 2, 3]
    assert payload["current"] == [1, 2, 3]
    assert backend.delete_calls == [("s1", 4)]


def test_websocket_prompt_carries_branch_fields() -> None:
    """WebSocket prompt 帧把 parent / regenerate 透传给后端。"""
    client, backend = _client()
    with client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_json()["type"] == "activity"
        socket.send_json(
            {
                "type": "prompt",
                "session_id": "s1",
                "text": "",
                "parent": 2,
                "regenerate": True,
            }
        )
        while True:
            frame: dict[str, Any] = socket.receive_json()
            if frame["type"] == "session":
                break
    assert backend.last_prompt == {
        "session_id": "s1",
        "text": "",
        "parent": 2,
        "regenerate": True,
    }


def test_rest_chat_carries_branch_fields() -> None:
    """REST 聊天接口同样接受 parent / regenerate。"""
    client, backend = _client()
    response = client.post(
        "/api/chat",
        json={
            "session_id": "s1",
            "text": "改过的问题",
            "parent": 1,
            "regenerate": False,
        },
    )
    assert response.status_code == 200
    assert backend.last_prompt["parent"] == 1
    assert backend.last_prompt["regenerate"] is False


def test_empty_message_is_rejected_unless_regenerating() -> None:
    """空消息照旧拒绝；但「重试」允许只发一个 regenerate 标记。"""
    client, _ = _client()
    assert client.post("/api/chat", json={"session_id": "s1"}).status_code == 422
    allowed = client.post("/api/chat", json={"session_id": "s1", "regenerate": True})
    assert allowed.status_code == 200


def test_rename_branch_sets_label() -> None:
    """PATCH /branches/{node} 给分支起名，视图里能读到。"""
    client, backend = _client()
    response: Any = client.patch(
        "/api/sessions/s1/branches/3", json={"label": "  严谨 版 "}
    )
    assert response.status_code == 200
    assert backend.label_calls == [(3, "  严谨 版 ")]
    payload = response.json()
    labels = {node["id"]: node["label"] for node in payload["nodes"]}
    assert labels[3] == "严谨 版"


def test_rename_branch_clears_label() -> None:
    """传空串 = 恢复序号显示。"""
    client, _ = _client()
    client.patch("/api/sessions/s1/branches/3", json={"label": "临时"})
    response: Any = client.patch("/api/sessions/s1/branches/3", json={"label": ""})
    assert response.status_code == 200
    labels = {node["id"]: node["label"] for node in response.json()["nodes"]}
    assert labels[3] == ""
