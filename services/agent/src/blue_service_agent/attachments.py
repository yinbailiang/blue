"""附件装配与 materialize：把伪 URL 还原成可发给模型的内容。

消息里存的是 ``blue+attachment://<sha256>``，而模型只接受 ``data:`` URL 或
http(s) 链接，因此发请求前必须换一次（materialize）。换不出来时降级成文本占位，
绝不把伪 URL 递给模型——那会直接被服务端 400。
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from blue_contracts_session import (
    SessionAttachment,
    SessionAttachmentBlob,
    attachment_url,
    normalize_base64,
    parse_attachment_url,
    sha256_hex,
)
from blue_contracts_tool import ToolAttachment
from infinity_agent import Message, MultiModalContent

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FALLBACK",
    "AttachmentMaterializer",
    "build_user_message",
    "tool_attachment_parts",
]

DEFAULT_FALLBACK = "（附件已附加，但内容不可用）"
"""materialize 失败时的文本占位。"""

FetchDataUrl = Callable[[str], Awaitable[str | None]]
"""按哈希取 data URL；取不到返回 ``None``。"""


def build_user_message(
    text: str, attachments: Iterable[SessionAttachment]
) -> tuple[Message, list[SessionAttachmentBlob]]:
    """把用户文本与附件装配成一条 user 消息。

    附件按内容寻址：这里算哈希、生成伪 URL，并把原始字节作为 blob 一起返回，
    由 :meth:`SessionGateway.append` 在同一事务里落库（天然去重）。

    :param text: 用户文本，可为空
    :param attachments: prompt 里内联的附件
    :return: （消息, 需要落库的 blob 列表）

    :raises ValueError: 附件 base64 非法
    """
    blobs: list[SessionAttachmentBlob] = []
    pending = list(attachments)
    if not pending:
        # 没有附件时保持纯字符串内容，既有行为不变
        return Message.user(text), blobs
    parts: list[MultiModalContent] = []
    seen: set[str] = set()
    if text.strip():
        parts.append(MultiModalContent.text_part(text))
    for attachment in pending:
        payload, declared = normalize_base64(attachment.data)
        data = base64.b64decode(payload)
        digest = sha256_hex(data)
        mime = (declared or attachment.mime or "application/octet-stream").strip()
        if mime.startswith("image/"):
            parts.append(MultiModalContent.image_part(attachment_url(digest)))
        else:
            parts.append(
                MultiModalContent.file_part(
                    file_data=attachment_url(digest),
                    filename=attachment.name,
                )
            )
        if digest not in seen:
            seen.add(digest)
            blobs.append(SessionAttachmentBlob(hash=digest, mime=mime, data=payload))
    content: str | list[MultiModalContent] = parts if parts else text
    return Message.user(content), blobs


@dataclass
class AttachmentMaterializer:
    """把消息里的附件伪 URL 换成 data URL（带进程内 LRU 缓存）。"""

    fetch: FetchDataUrl
    """按哈希取 data URL。"""

    max_entries: int = 256
    """缓存上限；超出后淘汰最久未使用的条目。"""

    _cache: dict[str, str | None] = field(default_factory=dict[str, str | None])

    async def resolve(self, digest: str) -> str | None:
        """取某个哈希对应的 data URL（命中缓存直接返回，并按 LRU 刷新顺序）。"""
        if digest in self._cache:
            hit = self._cache.pop(digest)
            self._cache[digest] = hit
            return hit
        data_url = await self.fetch(digest)
        if self.max_entries > 0 and len(self._cache) >= self.max_entries:
            self._cache.pop(next(iter(self._cache)))
        self._cache[digest] = data_url
        return data_url

    async def materialize(self, messages: Iterable[Message]) -> list[Message]:
        """返回可发给模型的消息副本（原列表不受影响）。"""
        return [await self._materialize_message(message) for message in messages]

    async def _materialize_message(self, message: Message) -> Message:
        """替换单条消息里的附件引用。"""
        content = message.content
        if not isinstance(content, list):
            return message
        parts = [await self._materialize_part(part) for part in content]
        return message.model_copy(update={"content": parts})

    async def _materialize_part(self, part: MultiModalContent) -> MultiModalContent:
        """替换单个内容块里的附件引用。"""
        image = part.image_url
        if image is not None:
            digest = parse_attachment_url(image.url)
            if digest is not None:
                data_url = await self.resolve(digest)
                if data_url is None:
                    return MultiModalContent.text_part(DEFAULT_FALLBACK)
                return part.model_copy(
                    update={"image_url": image.model_copy(update={"url": data_url})}
                )
        file = part.file
        if file is not None and file.file_data is not None:
            digest = parse_attachment_url(file.file_data)
            if digest is not None:
                data_url = await self.resolve(digest)
                if data_url is None:
                    return MultiModalContent.text_part(DEFAULT_FALLBACK)
                return part.model_copy(
                    update={"file": file.model_copy(update={"file_data": data_url})}
                )
        return part


def tool_attachment_parts(
    attachments: Iterable[ToolAttachment],
) -> tuple[list[MultiModalContent], list[SessionAttachmentBlob]]:
    """把工具返回的附件转成（内容块, 待落库 blob）。

    与用户附件一样走内容寻址：同图只落一份，消息里只留伪 URL。

    :param attachments: ``tool.result`` 里带回的附件
    :return: （内容块列表, 去重后的 blob 列表）
    """
    parts: list[MultiModalContent] = []
    blobs: list[SessionAttachmentBlob] = []
    seen: set[str] = set()
    for attachment in attachments:
        try:
            data = base64.b64decode(attachment.data, validate=True)
        except binascii.Error, ValueError:
            logger.warning("工具附件不是合法 base64，已跳过")
            continue
        digest = sha256_hex(data)
        if attachment.mime.startswith("image/"):
            parts.append(MultiModalContent.image_part(attachment_url(digest)))
        else:
            parts.append(
                MultiModalContent.file_part(
                    file_data=attachment_url(digest),
                    filename=attachment.name,
                )
            )
        if digest not in seen:
            seen.add(digest)
            blobs.append(
                SessionAttachmentBlob(
                    hash=digest,
                    mime=attachment.mime,
                    data=attachment.data,
                )
            )
    return parts, blobs
