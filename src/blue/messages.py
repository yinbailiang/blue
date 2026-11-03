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

from infinity_agent import Message

__all__ = [
    "decode_message",
    "decode_messages",
    "encode_message",
    "encode_messages",
]


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
