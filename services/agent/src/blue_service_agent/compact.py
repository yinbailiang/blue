"""历史压缩：把工具往返的长内容投影成「结构 + 省略标记」。

blue 的工具结果 97% 是 JSON，字段也规整，所以压缩按**字段**投影而不是猜文本：

- 状态类字段（``path`` / ``exit_code`` / ``success`` / ``bytes_written``）原样保留；
- 大文本字段（``stdout`` / ``content`` / ``stderr`` …）截成「开头 + 省略标记」，
  失败时额外保留结尾（错误信息通常在尾部）；
- ``write_file``／``edit_file`` 参数里的正文换成 ``<omitted N chars>``；
- 列表只留前几项，其余折叠成 ``…N more``。

字段该按什么策略处理，按优先级取：

1. **显式规则** ``CompactRule``（按工具名通配 + 字段路径）——运维可以精确指定；
2. **历史画像** ``FieldProfile``（``schema.py`` 从结果里归纳）——新工具、MCP 工具
   零配置自动适配；
3. **字段名启发式**（``BIG_TEXT_FIELDS`` / 长度阈值）——冷启动兜底。

两个约束：

1. **只压「较早」的消息**，最近 ``keep_messages`` 条原样发送——模型正在用的
   那份 ``read_file`` 结果被截断会直接导致它「看漏」；
2. **不改持久化数据**：返回新对象，库里始终是完整历史，需要细节时可以再读。

省略处写成 ``…[12345 chars omitted]`` 而不是静默丢弃：模型知道有东西没看到，
可以自己决定要不要重新读一遍。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, cast

from infinity_agent import FunctionCall, Message, MessageRole, ToolCall
from pydantic import BaseModel, Field

from blue_service_agent.schema import FieldProfile

__all__ = [
    "COMPACT_ERROR_CHARS",
    "COMPACT_KEEP_MESSAGES",
    "COMPACT_LIST_ITEMS",
    "COMPACT_TEXT_CHARS",
    "CompactRule",
    "compact_history",
    "estimate_chars",
    "learn_profiles",
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


class CompactRule(BaseModel):
    """按工具定制的压缩规则（字段路径用 ``a.b`` 表示嵌套）。"""

    tool: str = Field(default="*", description="工具名，可用 * 通配")
    drop: list[str] = Field(default_factory=list[str], description="直接省略的字段路径")
    keep: list[str] = Field(
        default_factory=list[str], description="始终原样保留的字段路径"
    )
    head: int | None = Field(default=None, ge=0, description="覆盖大文本保留的字符数")
    tail: int | None = Field(default=None, ge=0, description="覆盖失败时的尾部字符数")


@dataclass(frozen=True)
class _Limits:
    """一次投影的上下文：长度阈值 + 规则 + 历史画像。"""

    text_chars: int
    error_chars: int
    list_items: int
    drop: frozenset[str] = frozenset()
    keep: frozenset[str] = frozenset()
    profiles: Mapping[str, FieldProfile] = field(
        default_factory=dict[str, FieldProfile]
    )


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
    rules: Sequence[CompactRule] = (),
    profiles: Mapping[str, Mapping[str, FieldProfile]] | None = None,
) -> list[Message]:
    """返回压缩后的历史副本；尾部 ``keep_messages`` 条原样保留。

    :param rules: 按工具定制的规则（优先生效）。
    :param profiles: ``工具名 → 字段画像``，来自 :class:`SchemaLearner`。
    :return: 新列表；入参不会被修改。
    """
    source = list(messages)
    tools = _tool_names(source)
    split = _keep_start(source, keep_messages)
    compacted: list[Message] = []
    for index, message in enumerate(source):
        if index >= split:
            compacted.append(message)
            continue
        tool = tools.get(message.tool_call_id or "", "")
        limits = _limits_for(tool, rules, profiles, text_chars, error_chars, list_items)
        compacted.append(_compact_message(message, tool=tool, limits=limits))
    return compacted


def learn_profiles(
    messages: Iterable[Message],
) -> dict[str, dict[str, FieldProfile]]:
    """从历史工具结果归纳每个工具的字段画像（供 :func:`compact_history` 使用）。"""
    from blue_service_agent.schema import SchemaLearner

    source = list(messages)
    tools = _tool_names(source)
    learner = SchemaLearner()
    for message in source:
        if message.role != MessageRole.TOOL:
            continue
        tool = tools.get(message.tool_call_id or "", "")
        if tool:
            learner.observe_text(tool, _message_text(message))
    return {tool: learner.profiles(tool) for tool in learner.tools()}


def _limits_for(
    tool: str,
    rules: Sequence[CompactRule],
    profiles: Mapping[str, Mapping[str, FieldProfile]] | None,
    text_chars: int,
    error_chars: int,
    list_items: int,
) -> _Limits:
    """挑出这个工具适用的规则与画像。"""
    drop: set[str] = set()
    keep: set[str] = set()
    head = text_chars
    tail = error_chars
    for rule in rules:
        if not fnmatchcase(tool, rule.tool):
            continue
        drop.update(rule.drop)
        keep.update(rule.keep)
        if rule.head is not None:
            head = rule.head
        if rule.tail is not None:
            tail = rule.tail
    return _Limits(
        text_chars=head,
        error_chars=tail,
        list_items=list_items,
        drop=frozenset(drop - keep),
        keep=frozenset(keep),
        profiles=profiles.get(tool, {}) if profiles else {},
    )


def _tool_names(messages: Sequence[Message]) -> dict[str, str]:
    """建立 ``tool_call_id → 工具名`` 映射（工具结果消息本身不带名字）。"""
    names: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or []:
            names[call.id] = call.function.name
    return names


def _keep_start(messages: list[Message], keep_messages: int) -> int:
    """算出「从哪条开始原样保留」，并对齐 tool 配对边界。"""
    if keep_messages <= 0:
        return len(messages)
    start = max(0, len(messages) - keep_messages)
    while start > 0 and messages[start].role == MessageRole.TOOL:
        start -= 1
    return start


def _compact_message(message: Message, *, tool: str, limits: _Limits) -> Message:
    """按角色压缩一条消息。"""
    if message.role == MessageRole.TOOL:
        return _compact_tool_result(message, limits=limits)
    if message.role == MessageRole.ASSISTANT and message.tool_calls:
        return _compact_tool_arguments(message, limits=limits)
    return message


def _compact_tool_result(message: Message, *, limits: _Limits) -> Message:
    """压缩工具结果：JSON 走字段投影，纯文本走头尾截断。"""
    body = _message_text(message)
    if not body:
        return message
    try:
        data: object = json.loads(body)
    except json.JSONDecodeError:
        return message.model_copy(
            update={
                "content": _truncate_text(
                    body, text_chars=limits.text_chars, error_chars=0
                )
            }
        )
    projected = _project(
        data, failed=bool(_failure(data)), depth=0, path="", limits=limits
    )
    return message.model_copy(
        update={"content": json.dumps(projected, ensure_ascii=False)}
    )


def _compact_tool_arguments(message: Message, *, limits: _Limits) -> Message:
    """压缩 assistant 的 ``tool_calls`` 参数：写文件的正文直接省略。"""
    calls: list[ToolCall] = []
    changed = False
    for call in message.tool_calls or []:
        arguments: dict[str, Any] = dict(call.function.arguments)
        original = json.dumps(arguments, ensure_ascii=False)
        projected = _project(
            arguments,
            failed=False,
            depth=0,
            path="",
            limits=limits,
            drop=DROPPED_ARG_FIELDS,
        )
        projected_args: dict[str, Any] = (
            cast(dict[str, Any], projected)
            if isinstance(projected, dict)
            else arguments
        )
        if json.dumps(projected_args, ensure_ascii=False) != original:
            changed = True
        calls.append(
            ToolCall(
                id=call.id,
                function=FunctionCall(
                    name=call.function.name, arguments=projected_args
                ),
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
    path: str,
    limits: _Limits,
    drop: frozenset[str] = frozenset(),
) -> object:
    """按字段策略投影一个 JSON 值。"""
    if depth > MAX_DEPTH:
        return "<deep>"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            name = str(key)
            child = f"{path}.{name}" if path else name
            result[name] = _project(
                item,
                failed=failed,
                depth=depth + 1,
                path=child,
                limits=limits,
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
                path=path,
                limits=limits,
                drop=drop,
            )
            for item in items[: limits.list_items]
        ]
        if len(items) <= limits.list_items:
            return head
        return [*head, f"…{len(items) - limits.list_items} more"]
    if isinstance(value, str):
        return _project_text(value, failed=failed, path=path, limits=limits, drop=drop)
    return value


def _project_text(
    text: str,
    *,
    failed: bool,
    path: str,
    limits: _Limits,
    drop: frozenset[str],
) -> str:
    """决定一段字符串该保留、截断还是省略。"""
    name = path.rsplit(".", 1)[-1]
    if path in limits.keep or name in limits.keep:
        return text
    if len(text) <= limits.text_chars:
        return text
    if path in limits.drop or path in drop or name in drop:
        return f"<omitted {len(text)} chars>"
    profile = limits.profiles.get(name)
    if profile is not None:
        if profile.is_text:
            return _truncate_text(
                text,
                text_chars=limits.text_chars,
                error_chars=limits.error_chars if failed else 0,
            )
        if profile.keep:
            return text
    if name in BIG_TEXT_FIELDS or len(text) > SHORT_TEXT:
        return _truncate_text(
            text,
            text_chars=limits.text_chars,
            error_chars=limits.error_chars if failed else 0,
        )
    return text


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
    mapping = cast(dict[str, Any], data)
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
