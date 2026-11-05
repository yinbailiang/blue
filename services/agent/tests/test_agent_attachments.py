"""多模态附件测试：装配、materialize 与 agent 端到端链路。"""

from __future__ import annotations

import asyncio
import base64

from blue_contracts_session import (
    SessionAttachment,
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
    attachment_url,
    parse_attachment_url,
    sha256_hex,
)
from blue_contracts_tool import ToolAttachment
from blue_service_agent.agent import AgentCore
from blue_service_agent.attachments import (
    DEFAULT_FALLBACK,
    AttachmentMaterializer,
    build_user_message,
    tool_attachment_parts,
)
from blue_service_agent.config import AgentConfig
from blue_service_agent.history import sanitize_history
from blue_service_registry import ToolRegistryService
from infinity_agent import Message, MessageRole, MultiModalContent
from infinity_bus.templates.request import request
from tests.support import MemorySessionGateway, ScriptedClient, make_runtime

from blue.transport import MemoryFanout

PNG = b"\x89PNG\r\n\x1a\nblue-image"
"""测试用图片字节。"""

PNG_MIME = "image/png"


def _attachment(data: bytes = PNG, mime: str = PNG_MIME) -> SessionAttachment:
    """构造一个内联附件（不带 data: 前缀，验证裸 base64 也能用）。"""
    return SessionAttachment(
        mime=mime, data=base64.b64encode(data).decode("ascii"), name="a.png"
    )


def test_build_user_message_with_image_attachment() -> None:
    """文本 + 图片 → 两个内容块 + 一份待落库 blob。"""
    message, blobs = build_user_message("看图", [_attachment()])
    assert message.role == MessageRole.USER
    content = message.content
    assert isinstance(content, list)
    assert content[0].text == "看图"
    assert content[1].image_url is not None
    digest = parse_attachment_url(content[1].image_url.url)
    assert digest == sha256_hex(PNG)
    assert len(blobs) == 1
    assert blobs[0].hash == digest
    assert blobs[0].mime == PNG_MIME
    assert base64.b64decode(blobs[0].data) == PNG


def test_build_user_message_without_text_keeps_only_attachment() -> None:
    """只发图不带文字也合法。"""
    message, _ = build_user_message("   ", [_attachment()])
    content = message.content
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0].image_url is not None


def test_build_user_message_deduplicates_same_content() -> None:
    """同一张图重复上传只落一份 blob，但内容块各留一个。"""
    message, blobs = build_user_message("x", [_attachment(), _attachment()])
    content = message.content
    assert isinstance(content, list)
    assert len(content) == 3
    assert len(blobs) == 1


def test_build_user_message_non_image_becomes_file_part() -> None:
    """非图片按 file 块承载（DeepSeek 的 file 内容块）。"""
    message, blobs = build_user_message(
        "", [_attachment(b"%PDF-1.7", "application/pdf")]
    )
    content = message.content
    assert isinstance(content, list)
    part = content[0]
    assert part.file is not None
    assert part.file.file_data is not None
    assert parse_attachment_url(part.file.file_data) == sha256_hex(b"%PDF-1.7")
    assert part.file.filename == "a.png"
    assert blobs[0].mime == "application/pdf"


def test_build_user_message_plain_text_stays_string() -> None:
    """没有附件时消息内容仍是纯字符串（保持既有行为）。"""
    message, blobs = build_user_message("就一句话", [])
    assert message.content == "就一句话"
    assert blobs == []


def test_materializer_replaces_pseudo_url_with_data_url() -> None:
    """伪 URL 被换成 data URL，且同一哈希只取一次。"""
    asyncio.run(_materialize())


async def _materialize() -> None:
    digest = sha256_hex(PNG)
    calls: list[str] = []

    async def fetch(value: str) -> str | None:
        calls.append(value)
        return f"data:{PNG_MIME};base64,{base64.b64encode(PNG).decode('ascii')}"

    materializer = AttachmentMaterializer(fetch=fetch)
    message = Message.user([MultiModalContent.image_part(attachment_url(digest))])
    first = await materializer.materialize([message, message])
    second = await materializer.materialize([message])

    for result in (first[0], first[1], second[0]):
        content = result.content
        assert isinstance(content, list)
        assert content[0].image_url is not None
        assert content[0].image_url.url.startswith("data:image/png;base64,")
    assert calls == [digest]
    # 原消息不被就地修改
    original = message.content
    assert isinstance(original, list)
    assert original[0].image_url is not None
    assert parse_attachment_url(original[0].image_url.url) == digest


def test_materializer_degrades_when_attachment_missing() -> None:
    """取不到附件时降级成文本占位，绝不把伪 URL 发给模型。"""
    asyncio.run(_degrade())


async def _degrade() -> None:
    async def fetch(_digest: str) -> str | None:
        return None

    materializer = AttachmentMaterializer(fetch=fetch)
    message = Message.user(
        [MultiModalContent.image_part(attachment_url(sha256_hex(PNG)))]
    )
    result = await materializer.materialize([message])
    content = result[0].content
    assert isinstance(content, list)
    assert content[0].text == DEFAULT_FALLBACK
    assert content[0].image_url is None


def test_materializer_handles_file_parts() -> None:
    """file 块的 file_data 同样会被 materialize。"""
    asyncio.run(_materialize_file())


async def _materialize_file() -> None:
    digest = sha256_hex(b"%PDF")

    async def fetch(_digest: str) -> str | None:
        return "data:application/pdf;base64,JVBERg=="

    message = Message.user(
        [
            MultiModalContent.file_part(
                file_data=attachment_url(digest), filename="a.pdf"
            )
        ]
    )
    result = await AttachmentMaterializer(fetch=fetch).materialize([message])
    content = result[0].content
    assert isinstance(content, list)
    assert content[0].file is not None
    assert content[0].file.file_data == "data:application/pdf;base64,JVBERg=="


def test_sanitize_history_keeps_multimodal_user_message() -> None:
    """历史上含附件的 user 消息不会被当成空消息丢掉。"""
    user = Message.user([MultiModalContent.image_part(attachment_url("f" * 64))])
    cleaned = sanitize_history([Message.system("sys"), user])
    assert [message.role for message in cleaned] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
    ]


def test_agent_stores_pseudo_url_and_sends_data_url() -> None:
    """端到端：库里存伪 URL，发给模型的是 data URL。"""
    asyncio.run(_agent_round_trip())


async def _agent_round_trip() -> None:
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    client = ScriptedClient([Message.assistant(content="看到了")])
    gateway = MemorySessionGateway()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=2),
        client=client,
        session_gateway=gateway,
    )
    chat = make_runtime(hub, "chat")
    await registry.start()
    await core.start()
    await chat.start()
    try:
        reply = await request(
            chat.proxy(),
            SessionPromptEvent.name,
            {
                "text": "看图",
                "attachments": [
                    {
                        "mime": PNG_MIME,
                        "data": base64.b64encode(PNG).decode("ascii"),
                    }
                ],
            },
            SessionReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(reply, SessionReplyResponse)
        assert reply.text == "看到了"
    finally:
        await chat.stop()
        await core.stop()
        await registry.stop()

    digest = sha256_hex(PNG)
    stored = gateway.messages["s1"]
    user_message = stored[1]
    content = user_message.content
    assert isinstance(content, list)
    assert content[1].image_url is not None
    assert parse_attachment_url(content[1].image_url.url) == digest
    assert digest in gateway.blobs

    sent = client.calls[0]
    sent_content = sent[-1].content
    assert isinstance(sent_content, list)
    assert sent_content[1].image_url is not None
    assert sent_content[1].image_url.url.startswith("data:image/png;base64,")


def test_tool_attachment_parts_builds_pseudo_urls_and_blobs() -> None:
    """工具附件转成伪 URL 内容块 + 去重后的 blob。"""
    data = base64.b64encode(PNG).decode("ascii")
    parts, blobs = tool_attachment_parts(
        [
            ToolAttachment(mime=PNG_MIME, data=data),
            ToolAttachment(mime=PNG_MIME, data=data),
            ToolAttachment(
                mime="application/pdf", data=base64.b64encode(b"%PDF").decode()
            ),
        ]
    )
    assert [part.image_url is not None for part in parts] == [True, True, False]
    assert parts[2].file is not None
    digest = sha256_hex(PNG)
    first = parts[0].image_url
    assert first is not None
    assert parse_attachment_url(first.url) == digest
    assert [blob.hash for blob in blobs] == [digest, sha256_hex(b"%PDF")]


def test_tool_attachment_parts_skips_bad_base64() -> None:
    """非法 base64 跳过而不是抛错。"""
    parts, blobs = tool_attachment_parts([ToolAttachment(mime=PNG_MIME, data="!!!")])
    assert parts == []
    assert blobs == []


def test_materializer_caches_a_working_set_bigger_than_64() -> None:
    """工作集超过旧上限 64 时也只取一遍（上限已提到 256）。"""
    asyncio.run(_big_working_set())


async def _big_working_set() -> None:
    calls: list[str] = []

    async def fetch(digest: str) -> str:
        calls.append(digest)
        return "data:image/png;base64,AAAA"

    materializer = AttachmentMaterializer(fetch=fetch)
    messages = [
        Message.user(
            [MultiModalContent.image_part(attachment_url(sha256_hex(bytes([i]))))]
        )
        for i in range(65)
    ]
    for _ in range(3):
        await materializer.materialize(messages)

    assert len(calls) == 65
    assert len(set(calls)) == 65


def test_materializer_evicts_least_recently_used() -> None:
    """缓存满时淘汰最久未使用的：刚命中过的条目不会被丢掉。"""
    asyncio.run(_evict_least_recently_used())


async def _evict_least_recently_used() -> None:
    calls: list[str] = []

    async def fetch(digest: str) -> str:
        calls.append(digest)
        return "data:image/png;base64,AAAA"

    digests = {name: sha256_hex(name.encode()) for name in ("a", "b", "c")}
    materializer = AttachmentMaterializer(fetch=fetch, max_entries=2)

    await materializer.resolve(digests["a"])
    await materializer.resolve(digests["b"])
    await materializer.resolve(digests["a"])  # 命中 → 挪到最新
    await materializer.resolve(digests["c"])  # 容量满 → 淘汰 b
    await materializer.resolve(digests["a"])  # a 仍应命中

    assert calls == [digests["a"], digests["b"], digests["c"]]


def test_materializer_with_unbounded_cache() -> None:
    """``max_entries=0`` 表示不设上限：重复取同一张图不再发 RPC。"""
    asyncio.run(_unbounded())


async def _unbounded() -> None:
    calls: list[str] = []

    async def fetch(digest: str) -> str:
        calls.append(digest)
        return "data:image/png;base64,AAAA"

    materializer = AttachmentMaterializer(fetch=fetch, max_entries=0)
    digests = [sha256_hex(name.encode()) for name in ("a", "b", "c")]
    for _ in range(2):
        for digest in digests:
            await materializer.resolve(digest)

    assert calls == digests
