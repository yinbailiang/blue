"""历史压缩的预算层：超预算时整步丢掉最旧的对话，并留一句省略说明。"""

from __future__ import annotations

import json

from blue_service_agent.compact import compact_history, estimate_chars
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall


def _user(text: str) -> Message:
    """一条用户消息。"""
    return Message.user(text)


def _pairs(rounds: int, *, size: int) -> list[Message]:
    """造 ``rounds`` 步「工具往返」：提问 → 调工具 → 大段 JSON 结果。"""
    body = json.dumps({"rows": ["x" * size] * 3}, ensure_ascii=False)
    out: list[Message] = []
    for index in range(rounds):
        out.append(_user(f"第 {index} 轮"))
        out.append(
            Message.assistant(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call-{index}",
                        function=FunctionCall(
                            name="run_shell", arguments={"command": "ls"}
                        ),
                    )
                ],
            )
        )
        out.append(Message.tool(content=body, tool_call_id=f"call-{index}"))
    return out


def _note_of(messages: list[Message]) -> str:
    """取出那条省略说明（没有则返回空串）。"""
    for message in messages:
        content = str(message.content or "")
        if message.role == MessageRole.USER and "已省略" in content:
            return content
    return ""


def _pairing_is_intact(messages: list[Message]) -> bool:
    """每个 tool 结果都还能找到它对应的 tool_call。"""
    pending: set[str] = set()
    for message in messages:
        if message.role == MessageRole.ASSISTANT:
            pending = {call.id for call in message.tool_calls or []}
        elif message.role == MessageRole.TOOL and message.tool_call_id not in pending:
            return False
        elif message.role == MessageRole.TOOL:
            pending.discard(str(message.tool_call_id))
    return True


def test_history_under_budget_is_left_alone() -> None:
    """没超预算就什么都不做（对象都不换）。"""
    history = _pairs(3, size=100)
    out = compact_history(
        history, keep_messages=100, text_chars=200, target_chars=10_000_000
    )
    assert out == history


def test_zero_budget_keeps_the_previous_behaviour() -> None:
    """target_chars=0 表示不限：只走按字段投影那一层，行为与旧版一致。"""
    history = _pairs(5, size=3000)
    out = compact_history(history, keep_messages=2, text_chars=200, target_chars=0)
    assert "<elided" not in "".join(str(m.content or "") for m in out)
    assert any(
        '"…' in str(m.content or "") or "…[" in str(m.content or "") for m in out
    )


def test_over_budget_drops_whole_oldest_steps() -> None:
    """超预算：整步丢掉最旧的对话，并留一句省略说明（不是逐条截成一行）。"""
    history = _pairs(20, size=2000)
    whole = estimate_chars(compact_history(history, keep_messages=4, text_chars=200))
    fitted = compact_history(
        history, keep_messages=4, text_chars=200, target_chars=whole // 2
    )

    assert estimate_chars(fitted) <= whole // 2
    assert _note_of(fitted), "应当留下省略说明"
    assert _pairing_is_intact(fitted), "剩下的 tool 结果必须仍有对应 tool_call"
    # 没有「截一刀」的噪音：保留下来的消息都是完整内容
    assert "<elided" not in "".join(str(m.content or "") for m in fitted)


def test_dropped_note_sits_right_after_the_system_prompt() -> None:
    """省略说明紧跟在系统提示之后：模型一眼看出断点在哪。"""
    history = [Message.system("你是测试"), *_pairs(20, size=2000)]
    whole = estimate_chars(compact_history(history, keep_messages=4, text_chars=200))
    fitted = compact_history(
        history, keep_messages=4, text_chars=200, target_chars=whole // 2
    )
    assert fitted[0].role == MessageRole.SYSTEM
    assert "已省略" in str(fitted[1].content)


def test_last_step_is_never_dropped() -> None:
    """预算压到再小，最后一步（当前这轮的输入）也要留着。"""
    history = [*_pairs(10, size=2000), _user("现在的问题")]
    fitted = compact_history(history, keep_messages=4, text_chars=200, target_chars=100)
    assert fitted[-1].content == "现在的问题"


def test_boundary_is_quantised_so_the_prefix_stays_stable() -> None:
    """投影分界按 50 条取整：多来一条消息，压缩结果的前缀不会跟着挪一格。"""
    history = [_user(f"第 {i} 轮") for i in range(130)]
    first = compact_history(history, keep_messages=50, text_chars=200)
    second = compact_history(
        [*history, _user("再来一条")], keep_messages=50, text_chars=200
    )
    assert first[:50] == second[:50]
    assert len(second) == len(history) + 1


def _assistant_with_reasoning(reasoning: str) -> Message:
    """造一条带思维链的助手消息。"""
    return Message.assistant(content="好", reasoning_content=reasoning)


def test_estimate_counts_reasoning() -> None:
    """思维链必须计入预算：带 tools 时它会被拼进上下文（DeepSeek 文档）。

    漏算过一次：这条会话"正文 468k tok + 思维链 235k tok"，但按字符估算只看到
    正文，预算等于虚高了 33%。
    """
    without = Message.assistant(content="好")
    with_reasoning = _assistant_with_reasoning("x" * 500)
    assert estimate_chars([with_reasoning]) == estimate_chars([without]) + 500
