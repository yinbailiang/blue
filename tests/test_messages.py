"""Message JSON 编解码测试：tool_calls 参数必须可 round-trip。"""

from __future__ import annotations

import pytest
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall

from blue.messages import decode_message, decode_messages, encode_message


def test_roundtrip_plain_message() -> None:
    """普通消息可往返。"""
    message = Message.assistant(content="你好")

    restored = decode_message(encode_message(message))

    assert restored.role is MessageRole.ASSISTANT
    assert restored.content == "你好"


def test_roundtrip_tool_call_arguments() -> None:
    """tool_calls 的 arguments 以对象形式还原。"""
    message = Message.assistant(
        tool_calls=[
            ToolCall(
                id="call-1",
                function=FunctionCall(name="echo", arguments={"text": "hi"}),
            )
        ]
    )

    restored = decode_message(encode_message(message))

    assert restored.tool_calls is not None
    arguments = restored.tool_calls[0].function.arguments
    assert arguments == {"text": "hi"}


def test_roundtrip_tool_message() -> None:
    """tool 消息保留 tool_call_id 与内容。"""
    message = Message.tool(content='{"ok": true}', tool_call_id="call-1")

    restored = decode_message(encode_message(message))

    assert restored.role is MessageRole.TOOL
    assert restored.tool_call_id == "call-1"
    assert restored.content == '{"ok": true}'


def test_decode_rejects_invalid_json() -> None:
    """非法 JSON 给出明确错误。"""
    with pytest.raises(ValueError, match="JSON"):
        decode_message("not-json")


def test_decode_messages_batch() -> None:
    """批量解码保持顺序。"""
    payloads = [
        encode_message(Message.user("a")),
        encode_message(Message.assistant(content="b")),
    ]
    messages = decode_messages(payloads)
    assert [message.content for message in messages] == ["a", "b"]
