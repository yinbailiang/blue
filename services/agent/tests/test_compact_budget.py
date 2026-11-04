"""历史压缩的预算层：超预算时把最旧的消息降级成一行。"""

from __future__ import annotations

import json

from blue_service_agent.compact import SLIM_CHARS, compact_history, estimate_chars
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall


def _user(text: str) -> Message:
    """一条用户消息。"""
    return Message.user(text)


def _pairs(rounds: int, *, size: int) -> list[Message]:
    """造历史：每轮「用户 → 助手调工具 → 工具结果 → 助手总结」。"""
    body = "x" * size
    messages: list[Message] = [Message.system("你是 blue，一个会调用工具的 Agent。")]
    for index in range(rounds):
        call_id = f"call-{index}"
        messages.append(Message.user(f"第 {index} 轮：请跑一下命令"))
        messages.append(
            Message.assistant(
                content=f"第 {index} 轮的解释：" + "说明" * 20,
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        function=FunctionCall(
                            name="run_shell", arguments={"command": body}
                        ),
                    )
                ],
            )
        )
        messages.append(
            Message.tool(
                content=json.dumps(
                    {"exit_code": 0, "stdout": body, "command": body},
                    ensure_ascii=False,
                ),
                tool_call_id=call_id,
            )
        )
        messages.append(Message.assistant(content=f"第 {index} 轮完成。" + "结论" * 10))
    return messages


def test_budget_slims_the_oldest_and_spares_the_tail() -> None:
    """只要降级旧消息就够用时，最近保留的那几条一个字都不动。"""
    history = _pairs(30, size=2000)
    plain = compact_history(history, keep_messages=8, text_chars=200)
    before = estimate_chars(plain)
    target = before - 8_000  # 只需降级几条最旧的消息

    fitted = compact_history(
        history, keep_messages=8, text_chars=200, target_chars=target
    )
    assert estimate_chars(fitted) <= target
    assert len(fitted) == len(plain)  # 只降级、不删消息
    assert fitted[-8:] == plain[-8:], "尾部不该被降级"


def test_budget_is_hard_even_if_the_tail_must_shrink() -> None:
    """预算压到很小的时候，尾部也会被降级——预算是硬约束。"""
    history = _pairs(30, size=2000)
    plain = compact_history(history, keep_messages=8, text_chars=200)
    assert estimate_chars(plain) > 12_000
    target = 12_000  # 光降级旧消息不够，必须动尾部才压得下来

    fitted = compact_history(
        history, keep_messages=8, text_chars=200, target_chars=target
    )
    assert estimate_chars(fitted) <= target
    assert len(fitted) == len(plain)
    assert fitted[-8:] != plain[-8:], "预算不够时应继续降级尾部"


def test_budget_keeps_system_prompt_and_pairing() -> None:
    """系统提示保持原样；降级后 tool_call 与 tool 结果仍然配对。"""
    history = _pairs(20, size=2000)
    fitted = compact_history(
        history, keep_messages=4, text_chars=200, target_chars=10_000
    )
    assert fitted[0].role == MessageRole.SYSTEM
    assert fitted[0].content == history[0].content

    pending: set[str] = set()
    for message in fitted:
        for call in message.tool_calls or []:
            pending.add(call.id)
        if message.role == MessageRole.TOOL:
            assert message.tool_call_id in pending, "工具结果丢了对应的调用"
            pending.discard(message.tool_call_id or "")
    assert not pending, "有调用没有对应的结果"


def test_slimmed_tool_result_keeps_a_hint() -> None:
    """降级后的工具结果保留字符数与开头，不是静默丢弃。"""
    history = _pairs(12, size=2000)
    fitted = compact_history(
        history, keep_messages=2, text_chars=200, target_chars=8_000
    )
    slimmed = [
        message
        for message in fitted
        if message.role == MessageRole.TOOL
        and str(message.content).startswith("<elided")
    ]
    assert slimmed, "应当有工具结果被降级"
    text = str(slimmed[0].content)
    assert "chars>" in text
    assert len(text) <= SLIM_CHARS + 40


def test_zero_budget_keeps_the_previous_behaviour() -> None:
    """target_chars=0 时行为与旧版完全一致（只按条数压缩）。"""
    history = _pairs(6, size=500)
    assert compact_history(history, keep_messages=4) == compact_history(
        history, keep_messages=4, target_chars=0
    )


def test_history_under_budget_is_left_alone() -> None:
    """本来就在预算内的历史不做任何降级。"""
    history = _pairs(3, size=100)
    fitted = compact_history(history, keep_messages=4, target_chars=1_000_000)
    assert fitted == compact_history(history, keep_messages=4)


def test_boundary_is_quantised_so_the_prefix_stays_stable() -> None:
    """分界按 50 条取整：多来一条消息，压缩结果的前缀不会跟着改一格。"""
    history = [_user(f"第 {i} 轮") for i in range(130)]
    first = compact_history(history, keep_messages=50, text_chars=200)
    second = compact_history(
        [*history, _user("再来一条")], keep_messages=50, text_chars=200
    )
    # 130 条：分界 = (130-50)//50*50 = 50；131 条：同样 = 50 → 前 50 条一字不差
    assert first[:50] == second[:50]
    assert len(second) == len(history) + 1
