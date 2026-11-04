"""消息 JSON 编解码：在总线与会话存储之间安全传递 ``Message``。

``infinity_agent.Message`` 的 ``FunctionCall.arguments`` 有自定义
``field_serializer``，会把参数字典序列化成字符串；直接 ``model_validate``
该结构会失败。本模块在编码时把 ``arguments`` 还原为对象，解码时兼容字符串
形式，保证 tool_calls 可 round-trip。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from infinity_agent import Message, MessageRole, MultiModalContent

__all__ = [
    "PREVIEW_LIMIT",
    "TITLE_LIMIT",
    "condense",
    "decode_message",
    "decode_messages",
    "encode_message",
    "encode_messages",
    "message_text",
]

TITLE_LIMIT = 28
"""会话标题长度上限（超出截断加省略号）。"""

PREVIEW_LIMIT = 120
"""会话摘要长度上限。"""


def encode_message(message: Message) -> str:
    """把消息编码为稳定的 JSON 文本。"""
    data = message.model_dump(mode="json")
    _normalize_tool_calls(data, encode=True)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def decode_message(payload: str) -> Message:
    """从 JSON 文本解码消息。

    :raises ValueError: JSON 或消息结构非法。
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"消息 JSON 非法：{error}") from error
    if not isinstance(data, dict):
        raise ValueError("消息 JSON 必须是对象")
    normalized = cast(dict[str, Any], data)
    _normalize_tool_calls(normalized, encode=False)
    try:
        return Message.model_validate(normalized)
    except Exception as error:
        raise ValueError(f"消息结构非法：{error}") from error


def encode_messages(messages: Iterable[Message]) -> list[str]:
    """批量编码。"""
    return [encode_message(message) for message in messages]


def decode_messages(payloads: Iterable[str]) -> list[Message]:
    """批量解码。"""
    return [decode_message(payload) for payload in payloads]


def _normalize_tool_calls(data: dict[str, Any], *, encode: bool) -> None:
    """把 tool_calls.function.arguments 在字符串与对象之间转换。"""
    tool_calls = data.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for call in cast(list[Any], tool_calls):
        if not isinstance(call, dict):
            continue
        function = cast(dict[str, Any], call).get("function")
        if not isinstance(function, dict):
            continue
        function_data = cast(dict[str, Any], function)
        arguments = function_data.get("arguments")
        if encode and isinstance(arguments, str):
            try:
                function_data["arguments"] = json.loads(arguments)
            except json.JSONDecodeError:
                function_data["arguments"] = {}
        elif not encode and isinstance(arguments, str):
            try:
                function_data["arguments"] = json.loads(arguments)
            except json.JSONDecodeError:
                raise ValueError(
                    "tool_calls.function.arguments 不是合法 JSON"
                ) from None


def message_text(message: Message) -> str:
    """取出消息里可读的纯文本：多模态只取文本片段，图片记为 ``[图片]``。

    非用户消息（assistant 的 tool_calls、tool 结果）也可能带文本，
    这里只看 content，不做角色判断。
    """
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    pieces: list[str] = []
    for part in content:
        text = _part_text(part)
        if text:
            pieces.append(text)
    return " ".join(pieces)


def _part_text(part: MultiModalContent) -> str:
    if part.text:
        return part.text
    if part.image_url is not None:
        return "[图片]"
    if part.file is not None:
        return "[文件]"
    return ""


def condense(text: str, *, limit: int = TITLE_LIMIT) -> str:
    """把一段文本压成单行摘要：折叠空白、超长截断并加省略号。"""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def derive_title(messages: Iterable[Message], *, limit: int = TITLE_LIMIT) -> str:
    """按顺序找第一条有文本的用户消息，作为会话标题。"""
    for message in messages:
        if message.role is not MessageRole.USER:
            continue
        text = condense(message_text(message), limit=limit)
        if text:
            return text
    return ""


def derive_preview(messages: Iterable[Message], *, limit: int = PREVIEW_LIMIT) -> str:
    """取最后一条有文本的用户消息作为摘要。"""
    preview = ""
    for message in messages:
        if message.role is not MessageRole.USER:
            continue
        text = condense(message_text(message), limit=limit)
        if text:
            preview = text
    return preview
