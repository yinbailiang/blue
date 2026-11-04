"""历史压缩：把工具往返的长内容投影成「结构 + 省略标记」。

blue 的工具结果 97% 是 JSON，字段也很规整（``run_shell`` 永远是
command/stdout/exit_code…，``read_file`` 是 path/content）。所以压缩不必靠
文本启发式猜哪句重要，**按字段投影**即可：

- 状态类字段（``path`` / ``exit_code`` / ``success`` / ``bytes_written``）原样保留；
- 大文本字段（``stdout`` / ``content`` / ``stderr`` …）截成「开头 + 省略标记」，
  失败时额外保留结尾（错误信息通常在尾部）；
- ``write_file``／``edit_file`` 的参数里，正文对后续推理没有价值（文件已落盘），
  换成 ``<omitted N chars>``；
- 列表只留前几项，其余折叠成 ``…N more``。

两个约束：

1. **只压「较早」的消息**，最近 ``keep_messages`` 条原样发送——模型正在用的
   那份 ``read_file`` 结果被截断会直接导致它「看漏」。
2. **不改持久化数据**：返回新对象，库里始终是完整历史，需要细节时可以再读。

省略处写成 ``…[12345 chars omitted]`` 而不是静默丢弃：模型知道有东西没看到，
可以自己决定要不要重新读一遍。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from infinity_agent import FunctionCall, Message, MessageRole, ToolCall

__all__ = [
    "COMPACT_ERROR_CHARS",
    "COMPACT_KEEP_MESSAGES",
    "COMPACT_LIST_ITEMS",
    "COMPACT_TEXT_CHARS",
    "compact_history",
    "estimate_chars",
]

COMPACT_KEEP_MESSAGES = 60
"""尾部这么多条消息原样保留（约等于最近 2–3 轮工具往返）。"""

COMPACT_TEXT_CHARS = 200
"""成功结果里大文本字段保留的字符数。"""

COMPACT_ERROR_CHARS = 600
"""失败结果额外保留的结尾字符数（错误信息通常在尾部）。"""

COMPACT_LIST_ITEMS = 3
"""列表字段保留的条数。"""

MAX_DEPTH = 4
"""递归深度上限（再深的嵌套直接折叠）。"""

SHORT_TEXT = 400
"""短于这个长度的字符串一律不动。"""

BIG_TEXT_FIELDS = frozenset(
    {
        "content",
        "stdout",
        "stderr",
        "text",
        "output",
        "body",
        "data",
        "diff",
        "patch",
        "new_str",
        "old_str",
        "old_text",
        "new_text",
        "prompt",
    }
)
"""这些字段名视为「大文本」，超过阈值就截断。"""

DROPPED_ARG_FIELDS = frozenset({"content"})
"""工具参数里可以直接省略的字段（写文件的正文）。"""

FAILURE_KEYS = ("success", "ok")
"""值为 ``False`` 时视为失败，保留更多尾部。"""


def estimate_chars(messages: Iterable[Message]) -> int:
    """粗略估算一批消息的文本体量（用于日志对比）。"""
    total = 0
    for message in messages:
        total += len(_message_text(message))
        for call in message.tool_calls or []:
            total += len(json.dumps(call.function.arguments, ensure_ascii=False))
    return total


def compact_history(
    messages: Iterable[Message],
    *,
    keep_messages: int = COMPACT_KEEP_MESSAGES,
    text_chars: int = COMPACT_TEXT_CHARS,
    error_chars: int = COMPACT_ERROR_CHARS,
    list_items: int = COMPACT_LIST_ITEMS,
) -> list[Message]:
    """返回压缩后的历史副本；尾部 ``keep_messages`` 条原样保留。

    入参不会被修改：需要改动的消息都通过 ``model_copy`` 复制。
    """
    source = list(messages)
    split = _keep_start(source, keep_messages)
    compacted: list[Message] = []
    for index, message in enumerate(source):
        if index >= split:
            compacted.append(message)
            continue
        compacted.append(
            _compact_message(
                message,
                text_chars=text_chars,
                error_chars=error_chars,
                list_items=list_items,
            )
        )
    return compacted


def _keep_start(messages: list[Message], keep_messages: int) -> int:
    """算出「从哪条开始原样保留」，并对齐 tool 配对边界。

    往回退到一条干净的起点（不是孤立的 ``tool`` 结果），避免把
    ``assistant(tool_calls)`` 和它的结果拆到压缩/未压缩两侧。
    """
    if keep_messages <= 0:
        return len(messages)
    start = max(0, len(messages) - keep_messages)
    while start > 0 and messages[start].role == MessageRole.TOOL:
        start -= 1
    return start


def _compact_message(
    message: Message,
    *,
    text_chars: int,
    error_chars: int,
    list_items: int,
) -> Message:
    """按角色压缩一条消息。"""
    if message.role == MessageRole.TOOL:
        return _compact_tool_result(
            message,
            text_chars=text_chars,
            error_chars=error_chars,
            list_items=list_items,
        )
    if message.role == MessageRole.ASSISTANT and message.tool_calls:
        return _compact_tool_arguments(
            message,
            text_chars=text_chars,
            error_chars=error_chars,
            list_items=list_items,
        )
    return message


def _compact_tool_result(
    message: Message, *, text_chars: int, error_chars: int, list_items: int
) -> Message:
    """压缩工具结果：JSON 走字段投影，纯文本走头尾截断。"""
    body = _message_text(message)
    if not body:
        return message
    try:
        data: object = json.loads(body)
    except json.JSONDecodeError:
        return message.model_copy(
            update={
                "content": _truncate_text(body, text_chars=text_chars, error_chars=0)
            }
        )
    failed = bool(_failure(data))
    projected = _project(
        data,
        failed=failed,
        depth=0,
        text_chars=text_chars,
        error_chars=error_chars,
        list_items=list_items,
    )
    return message.model_copy(
        update={"content": json.dumps(projected, ensure_ascii=False)}
    )


def _compact_tool_arguments(
    message: Message, *, text_chars: int, error_chars: int, list_items: int
) -> Message:
    """压缩 assistant 的 ``tool_calls`` 参数：写文件的正文直接省略。"""
    calls: list[ToolCall] = []
    changed = False
    for call in message.tool_calls or []:
        arguments = dict(call.function.arguments)
        original = json.dumps(arguments, ensure_ascii=False)
        projected = cast(
            dict[str, Any],
            _project(
                arguments,
                failed=False,
                depth=0,
                text_chars=text_chars,
                error_chars=error_chars,
                list_items=list_items,
                drop=DROPPED_ARG_FIELDS,
            ),
        )
        if json.dumps(projected, ensure_ascii=False) != original:
            changed = True
        calls.append(
            ToolCall(
                id=call.id,
                function=FunctionCall(name=call.function.name, arguments=projected),
            )
        )
    if not changed:
        return message
    return message.model_copy(update={"tool_calls": calls})


def _project(
    value: object,
    *,
    failed: bool,
    depth: int,
    text_chars: int,
    error_chars: int,
    list_items: int,
    drop: frozenset[str] | None = None,
) -> object:
    """按字段策略投影一个 JSON 值。"""
    if depth > MAX_DEPTH:
        return "<deep>"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            name = str(key)
            if (
                drop
                and name in drop
                and isinstance(item, str)
                and len(item) > text_chars
            ):
                result[name] = f"<omitted {len(item)} chars>"
                continue
            if isinstance(item, str) and (
                name in BIG_TEXT_FIELDS or len(item) > SHORT_TEXT
            ):
                result[name] = _truncate_text(
                    item,
                    text_chars=text_chars,
                    error_chars=error_chars if failed else 0,
                )
                continue
            result[name] = _project(
                item,
                failed=failed,
                depth=depth + 1,
                text_chars=text_chars,
                error_chars=error_chars,
                list_items=list_items,
                drop=drop,
            )
        return result
    if isinstance(value, list):
        items = cast(list[object], value)
        head = [
            _project(
                item,
                failed=failed,
                depth=depth + 1,
                text_chars=text_chars,
                error_chars=error_chars,
                list_items=list_items,
                drop=drop,
            )
            for item in items[:list_items]
        ]
        if len(items) <= list_items:
            return head
        return [*head, f"…{len(items) - list_items} more"]
    if isinstance(value, str) and len(value) > SHORT_TEXT:
        return _truncate_text(
            value, text_chars=text_chars, error_chars=error_chars if failed else 0
        )
    return value


def _truncate_text(text: str, *, text_chars: int, error_chars: int) -> str:
    """截断长文本：保留开头；``error_chars > 0`` 时额外保留结尾。"""
    if len(text) <= text_chars:
        return text
    head = text[:text_chars]
    if error_chars <= 0:
        return f"{head}…[{len(text)} chars omitted]"
    tail = text[-error_chars:]
    return f"{head}…[{len(text)} chars]…{tail}"


def _failure(data: object) -> bool:
    """从 JSON 结果里判断这次调用是否失败。"""
    if not isinstance(data, dict):
        return False
    mapping = cast(dict[str, object], data)
    for key in FAILURE_KEYS:
        if mapping.get(key) is False:
            return True
    exit_code = mapping.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    return bool(mapping.get("error"))


def _message_text(message: Message) -> str:
    """取消息的可读文本（多模态只取文本块）。"""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or []:
        if item.text:
            parts.append(item.text)
    return "\n".join(parts)
