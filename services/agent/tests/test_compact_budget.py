"""历史压缩的预算层：超预算时整步丢掉最旧的对话，并留一句省略说明。"""

from __future__ import annotations

import json

from blue_service_agent.compact import compact_history, estimate_chars
from infinity_agent import (
    FunctionCall,
    Message,
    MessageRole,
    MultiModalContent,
    ToolCall,
)


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


def _with_extras(history: list[Message], extra: int) -> list[Message]:
    """在原历史后面追加 ``extra`` 条新消息（模拟对话继续增长）。"""
    return [*history, *[_user(f"追加 {i}") for i in range(extra)]]


def _boundary(messages: list[Message], *, step: int, keep: int = 10) -> int:
    """测出「投影分界」：第一条原样保留的长工具结果的索引。

    长工具结果被投影后内容必然改变，所以「内容仍然相同」= 从这条起原样保留。
    找不到（分界之后已无工具结果）就返回消息总数。
    """
    compacted = compact_history(
        messages, keep_messages=keep, text_chars=200, boundary_step=step
    )
    for index, (before, after) in enumerate(zip(messages, compacted, strict=True)):
        if before.content == after.content and len(str(before.content)) > 1000:
            return index
    return len(compacted)


def test_boundary_is_quantised_so_the_prefix_stays_stable() -> None:
    """投影分界按步长取整：整个步长区间内分界不动，前缀缓存才命中得了。

    旧实现里这条测试用全短消息，压缩前后内容一样，断言恒真——分界其实每来一条
    就挪一格（``COMPACT_BOUNDARY_STEP`` 定义了却没人用）。这里改用长工具结果，
    让分界真的可观测。
    """
    step = 20
    history = _pairs(30, size=3000)
    boundaries = {
        _boundary(_with_extras(history, extra), step=step) for extra in range(step)
    }
    assert len(boundaries) == 1, boundaries


def test_boundary_step_zero_moves_every_message() -> None:
    """``boundary_step=0`` 关闭量化：分界逐条移动（回归到旧行为）。"""
    step = 0
    history = _pairs(30, size=3000)
    boundaries = {
        _boundary(_with_extras(history, extra), step=step) for extra in range(20)
    }
    assert len(boundaries) > 3, boundaries


def test_boundary_step_is_configurable() -> None:
    """步长越大，分界跨得越宽：同一份历史在 40 步长下还没跨格，20 步长下已经跨了。"""
    history = _pairs(30, size=3000)

    assert _boundary(_with_extras(history, 30), step=40) == 80
    assert _boundary(_with_extras(history, 30), step=20) > 80


def test_keep_zone_survives_normal_budget_pressure() -> None:
    """预算够用时丢弃止步于保留区：最近几轮原样，说明里不提「最近」。"""
    history = _pairs(20, size=2000)
    whole = estimate_chars(compact_history(history, keep_messages=4, text_chars=200))

    fitted = compact_history(
        history, keep_messages=4, text_chars=200, target_chars=whole * 5 // 6
    )

    assert "已省略" in _note_of(fitted)
    assert "最近" not in _note_of(fitted)
    assert fitted[-1].content == history[-1].content


def test_note_reports_recent_messages_when_keep_zone_is_eaten() -> None:
    """预算小到必须动用保留区时，说明里如实写出最近也被省略。"""
    history = _pairs(20, size=2000)
    whole = estimate_chars(compact_history(history, keep_messages=4, text_chars=200))

    fitted = compact_history(
        history, keep_messages=4, text_chars=200, target_chars=whole // 2
    )

    assert "最近" in _note_of(fitted)
    assert _pairing_is_intact(fitted)


def test_attachment_weight_can_trigger_budget_dropping() -> None:
    """附件按当量计入估算后，图片多的历史会真的进预算流程。"""
    images = [
        Message.user(
            [
                MultiModalContent.text_part("看图"),
                MultiModalContent.image_part(f"blue+attachment://{index:064x}"),
            ]
        )
        for index in range(10)
    ]

    ignored = compact_history(images, keep_messages=2, target_chars=10_000)
    counted = compact_history(
        images, keep_messages=2, target_chars=10_000, attachment_chars=2400
    )

    assert len(ignored) == len(images)
    assert len(counted) < len(images)
    assert "已省略" in _note_of(counted)


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
