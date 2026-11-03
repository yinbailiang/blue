"""WebUI 附件链路测试：上传、结构化序列化与取图端点。"""

from __future__ import annotations

import base64
from typing import Any, cast

from blue_contracts_session import (
    SessionAttachment,
    SessionInfo,
    SessionReplyResponse,
    SessionSnapshot,
    attachment_url,
    sha256_hex,
)
from blue_contracts_tool import CatalogQueryResponse
from blue_service_webui.app import create_app
from blue_service_webui.backend import (
    SystemView,
    WebBackend,
    serialize_message,
)
from blue_service_webui.config import WebUIConfig
from infinity_agent import Message, MultiModalContent
from starlette.testclient import TestClient

from blue.messages import encode_message

PNG = b"\x89PNG\r\n\x1a\nwebui-image"
PNG_MIME = "image/png"


class _Backend:
    """只实现附件链路所需能力的后端替身。"""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[str, bytes]] = {}
        self.prompts: list[tuple[str, str, list[SessionAttachment]]] = []

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> SessionReplyResponse:
        self.prompts.append((session_id, text, list(attachments or [])))
        return SessionReplyResponse(
            session_id=session_id, request_id="r", text="ok", steps=1
        )

    async def get_attachment(self, digest: str) -> tuple[str, bytes] | None:
        return self.blobs.get(digest)

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        del session_id
        return None

    async def sessions(self) -> list[SessionInfo]:
        return []

    async def reset(self, session_id: str) -> bool:
        del session_id
        return True

    async def cancel(self, session_id: str) -> bool:
        del session_id
        return False

    async def tools(self) -> CatalogQueryResponse:
        return CatalogQueryResponse(
            session_id="s1", request_id="catalog", version=0, tools=[]
        )

    async def system(self) -> SystemView:
        return SystemView()


def _client() -> tuple[Any, _Backend]:
    """构造测试客户端与后端替身（与 test_app.py 一致：TestClient 类型擦除）。"""
    backend = _Backend()
    app = create_app(backend=cast(WebBackend, backend), config=WebUIConfig())
    client: Any = TestClient(app)
    return client, backend


def test_chat_accepts_attachments() -> None:
    """REST 聊天接口把附件透传给后端。"""
    client, backend = _client()
    response = client.post(
        "/api/chat",
        json={
            "session_id": "s1",
            "text": "",
            "attachments": [
                {
                    "mime": PNG_MIME,
                    "data": base64.b64encode(PNG).decode("ascii"),
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["text"] == "ok"
    assert backend.prompts
    _, text, attachments = backend.prompts[0]
    assert text == ""
    assert len(attachments) == 1
    assert attachments[0].mime == PNG_MIME


def test_chat_rejects_empty_message() -> None:
    """文本与附件都为空时返回 422。"""
    client, _ = _client()
    response = client.post("/api/chat", json={"session_id": "s1", "text": "   "})
    assert response.status_code == 422


def test_attachment_endpoint_serves_bytes_with_cache_headers() -> None:
    """取图端点返回字节、正确 MIME 与不可变缓存头。"""
    client, backend = _client()
    digest = sha256_hex(PNG)
    backend.blobs[digest] = (PNG_MIME, PNG)

    response = client.get(f"/api/attachments/{digest}")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"].startswith(PNG_MIME)
    assert "immutable" in response.headers["cache-control"]


def test_attachment_endpoint_rejects_bad_hash() -> None:
    """哈希格式非法直接 400（不做模糊查询）。"""
    client, _ = _client()
    assert client.get("/api/attachments/not-a-hash").status_code == 400


def test_attachment_endpoint_missing_is_404() -> None:
    """不存在的哈希 404。"""
    client, _ = _client()
    assert client.get(f"/api/attachments/{'a' * 64}").status_code == 404


def test_serialize_message_exposes_parts_for_image() -> None:
    """多模态消息序列化出结构化 parts，附件给出可访问地址。"""
    digest = sha256_hex(PNG)
    message = Message.user(
        [
            MultiModalContent.text_part("看图"),
            MultiModalContent.image_part(attachment_url(digest)),
        ]
    )
    view = serialize_message(message)
    assert view.content == "看图"
    assert [part.type for part in view.parts] == ["text", "image"]
    assert view.parts[1].hash == digest
    assert view.parts[1].url == f"/api/attachments/{digest}"


def test_serialize_message_handles_file_part_and_history() -> None:
    """file 块也给出 parts；历史消息经编码往返后同样生效。"""
    digest = sha256_hex(b"%PDF")
    message = Message.user(
        [
            MultiModalContent.file_part(
                file_data=attachment_url(digest), filename="a.pdf"
            )
        ]
    )
    reloaded = encode_message(message)
    from blue.messages import decode_message

    view = serialize_message(decode_message(reloaded))
    assert [part.type for part in view.parts] == ["file"]
    assert view.parts[0].filename == "a.pdf"
    assert view.parts[0].url == f"/api/attachments/{digest}"
