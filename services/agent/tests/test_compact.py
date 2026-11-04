"""历史压缩测试：字段投影、尾轮保护、可逆性。"""

from __future__ import annotations

import json
from typing import Any, cast

from blue_service_agent.compact import compact_history, estimate_chars
from infinity_agent import FunctionCall, Message, ToolCall


def _json(message: Message) -> dict[str, Any]:
    """把工具结果消息解析成 dict（顺带收窄类型）。"""
    content = message.content
    assert isinstance(content, str)
    payload: object = json.loads(content)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _calls(message: Message) -> list[ToolCall]:
    """取出消息的 tool_calls（顺带收窄类型）。"""
    assert message.tool_calls is not None
    return message.tool_calls


def _tool(payload: object, *, call_id: str = "c1") -> Message:
    """构造一条工具结果消息。"""
    body = (
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    )
    return Message.tool(content=body, tool_call_id=call_id)


def _round(call_id: str, size: int) -> list[Message]:
    """造一轮真实结构：assistant(tool_calls) + 对应的 tool 结果。"""
    call = ToolCall(
        id=call_id,
        function=FunctionCall(
            name="write_file",
            arguments={"path": f"{call_id}.py", "content": "x" * size},
        ),
    )
    return [
        Message.assistant(tool_calls=[call]),
        Message.tool(content=json.dumps({"stdout": "y" * size}), tool_call_id=call_id),
    ]


def test_short_messages_are_untouched() -> None:
    """短消息原样返回（不做无谓复制）。"""
    messages = [Message.user("你好"), Message.assistant(content="好的")]

    compacted = compact_history(messages, keep_messages=0)

    assert compacted == messages
    assert compacted[0] is messages[0]


def test_large_stdout_is_truncated_with_marker() -> None:
    """成功结果的大 stdout 只留开头，并标注省略了多少字符。"""
    long_output = "line\n" * 2000
    message = _tool(
        {
            "command": "ls",
            "cwd": "/tmp",
            "exit_code": 0,
            "success": True,
            "stdout": long_output,
        }
    )

    compacted = compact_history([message], keep_messages=0)
    payload = _json(compacted[0])

    assert payload["command"] == "ls"
    assert payload["exit_code"] == 0
    assert payload["stdout"].endswith(f"…[{len(long_output)} chars omitted]")
    assert len(compacted[0].content or "") < len(long_output) / 4


def test_failed_result_keeps_tail() -> None:
    """失败结果额外保留结尾，错误信息通常在那里。"""
    tail = "Traceback: boom"
    long_output = "noise\n" * 2000 + tail
    message = _tool(
        {"command": "pytest", "exit_code": 1, "success": False, "stdout": long_output}
    )

    compacted = compact_history([message], keep_messages=0)
    stdout = _json(compacted[0])["stdout"]

    assert isinstance(stdout, str)
    assert stdout.endswith(tail)
    assert "chars]" in stdout


def test_tool_argument_content_is_omitted() -> None:
    """写文件的正文在历史里直接省略（文件已落盘）。"""
    body = "x" * 5000
    call = ToolCall(
        id="call-1",
        function=FunctionCall(
            name="write_file", arguments={"path": "a.py", "content": body}
        ),
    )
    message = Message.assistant(tool_calls=[call])

    compacted = compact_history([message], keep_messages=0)
    arguments = _calls(compacted[0])[0].function.arguments

    assert arguments["path"] == "a.py"
    assert arguments["content"] == f"<omitted {len(body)} chars>"
    # 原消息不受影响
    assert _calls(message)[0].function.arguments["content"] == body


def test_lists_are_folded() -> None:
    """长列表只留前几项。"""
    message = _tool({"results": [{"title": f"t{index}"} for index in range(10)]})

    compacted = compact_history([message], keep_messages=0)
    results = _json(compacted[0])["results"]

    assert isinstance(results, list)
    assert results[:3] == [{"title": "t0"}, {"title": "t1"}, {"title": "t2"}]
    assert results[-1] == "…7 more"


def test_recent_messages_kept_verbatim() -> None:
    """尾部 keep_messages 条一字不动（模型正在用）。"""
    messages = [*_round("old", 4000), *_round("r1", 4000), *_round("r2", 4000)]

    compacted = compact_history(messages, keep_messages=4)

    old_arguments = _calls(compacted[0])[0].function.arguments
    assert str(old_arguments["content"]).startswith("<omitted")
    assert old_arguments["path"] == "old.py"
    for original, result in zip(messages[2:], compacted[2:], strict=True):
        assert result is original


def test_keep_start_aligns_to_tool_boundary() -> None:
    """保留起点落在 tool 结果上时，往前扩到配对的 assistant。"""
    call = ToolCall(
        id="c1", function=FunctionCall(name="write_file", arguments={"path": "a"})
    )
    assistant = Message.assistant(tool_calls=[call])
    tool = _tool({"stdout": "x" * 5000})

    compacted = compact_history([assistant, tool], keep_messages=1)

    assert _calls(compacted[0])[0].function.arguments == {"path": "a"}
    assert compacted[1].content == tool.content


def test_non_json_tool_result_falls_back_to_text_truncation() -> None:
    """不是 JSON 的工具结果按纯文本截断，不报错。"""
    message = _tool("plain text " * 1000)

    compacted = compact_history([message], keep_messages=0)

    assert "chars omitted" in (compacted[0].content or "")
    assert len(compacted[0].content or "") < len(message.content or "")


def test_multimodal_message_is_not_broken() -> None:
    """多模态消息（内容为块列表）不被误伤。"""
    message = Message.user("一段带图片的说明")

    compacted = compact_history([message], keep_messages=0)

    assert compacted[0] is message


def test_estimate_chars_counts_tool_arguments() -> None:
    """体量估算包含 tool_calls 参数，便于日志对比。"""
    call = ToolCall(
        id="c1",
        function=FunctionCall(name="write_file", arguments={"content": "x" * 500}),
    )

    assert estimate_chars([Message.assistant(tool_calls=[call])]) > 500
