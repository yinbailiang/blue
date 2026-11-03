"""会话历史自愈测试：悬空 tool_calls、孤儿 tool 消息与空 assistant。"""

from __future__ import annotations

import json

from blue_service_agent.history import sanitize_history
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall


def _call(call_id: str, name: str = "echo") -> ToolCall:
    """构造一次工具调用。"""
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments={"x": 1}))


def _roles(messages: list[Message]) -> list[MessageRole]:
    """消息角色序列，便于断言结构。"""
    return [message.role for message in messages]


def test_sanitize_keeps_paired_history_untouched() -> None:
    """工具调用与结果一一对应时，历史原样返回。"""
    history = [
        Message.system("sys"),
        Message.user("hi"),
        Message.assistant(tool_calls=[_call("c1")]),
        Message.tool(content="ok", tool_call_id="c1"),
        Message.assistant(content="done"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == _roles(history)
    assert cleaned == history


def test_sanitize_fills_missing_tool_result() -> None:
    """悬空 tool_call 补写占位结果，且错误文本里带上工具名。"""
    history = [
        Message.system("sys"),
        Message.user("hi"),
        Message.assistant(tool_calls=[_call("c1", "shell_placeholder")]),
        Message.user("继续"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.USER,
    ]
    placeholder = cleaned[3]
    assert placeholder.tool_call_id == "c1"
    assert isinstance(placeholder.content, str)
    payload = json.loads(placeholder.content)
    assert "shell_placeholder" in payload["error"]


def test_sanitize_fills_only_missing_calls_and_keeps_order() -> None:
    """多个调用里只有部分有结果时，其余占位结果按声明顺序追加在已有结果之后。"""
    history = [
        Message.assistant(tool_calls=[_call("c1"), _call("c2"), _call("c3")]),
        Message.tool(content="r1", tool_call_id="c1"),
        Message.tool(content="r3", tool_call_id="c3"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.TOOL,
        MessageRole.TOOL,
    ]
    assert [message.tool_call_id for message in cleaned[1:]] == ["c1", "c3", "c2"]


def test_sanitize_drops_orphan_tool_message() -> None:
    """没有对应 tool_calls 的 tool 消息会被丢弃。"""
    history = [
        Message.user("hi"),
        Message.tool(content="r1", tool_call_id="ghost"),
        Message.assistant(content="done"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [MessageRole.USER, MessageRole.ASSISTANT]


def test_sanitize_drops_duplicate_tool_result() -> None:
    """同一个 tool_call_id 的重复结果只保留第一条。"""
    history = [
        Message.assistant(tool_calls=[_call("c1")]),
        Message.tool(content="first", tool_call_id="c1"),
        Message.tool(content="second", tool_call_id="c1"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert cleaned[1].content == "first"


def test_sanitize_drops_empty_assistant_message() -> None:
    """既无内容也无 tool_calls 的 assistant 消息无法发送，直接丢弃。"""
    history = [
        Message.user("hi"),
        Message.assistant(content=None),
        Message.assistant(content="   "),
        Message.assistant(content="done"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [MessageRole.USER, MessageRole.ASSISTANT]
    assert cleaned[1].content == "done"


def test_sanitize_keeps_reasoning_only_assistant_with_tool_calls() -> None:
    """只有思考内容、但带 tool_calls 的 assistant 消息必须保留。"""
    history = [
        Message.assistant(
            content=None, reasoning_content="想一想", tool_calls=[_call("c1")]
        ),
        Message.tool(content="r1", tool_call_id="c1"),
    ]

    cleaned = sanitize_history(history)

    assert _roles(cleaned) == [MessageRole.ASSISTANT, MessageRole.TOOL]


def test_sanitize_does_not_mutate_input() -> None:
    """纯函数：入参列表与元素均不被修改。"""
    history = [
        Message.assistant(tool_calls=[_call("c1")]),
        Message.user("继续"),
    ]
    original = list(history)

    sanitize_history(history)

    assert history == original
