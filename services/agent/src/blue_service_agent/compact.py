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
    "COMPACT_BOUNDARY_STEP",
    "COMPACT_ERROR_CHARS",
    "COMPACT_KEEP_MESSAGES",
    "COMPACT_LIST_ITEMS",
    "COMPACT_TEXT_CHARS",
    "DROPPED_NOTE",
    "NOTES_RESERVE",
    "RECENT_NOTE",
    "CompactRule",
    "compact_history",
    "count_attachments",
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

DROPPED_NOTE = (
    "[早期历史已省略：{steps} 步 / {messages} 条消息{recent}；"
    "需要细节请重新读取文件或重跑命令]"
)
"""超预算时插在原处的省略说明（不逐条截断，见 ``_fit_budget``）。"""

RECENT_NOTE = "（其中最近 {count} 条也一并省略）"
"""预算小到连「原样保留区」都要动用时，追加在那句说明后面的补充。"""

NOTES_RESERVE = 200
"""给省略说明预留的字符数，避免插进去又超预算。"""

COMPACT_BOUNDARY_STEP = 50
"""投影分界的量化步长默认值（可经 ``compact_boundary_step`` 配置）。

``keep_messages`` 的分界如果每来一条消息就往前挪一格，等于每次调用都把「第
len-keep 条」那条消息改写一遍：prompt 前缀从那里开始就和上次不同，上游的前缀
缓存会整段失效。把分界按步长取整，代价是最多少保护 ``步长-1`` 条，换来的是
缓存能连续命中 ``步长`` 轮。``0`` 表示不量化（逐条移动）。
"""

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


def estimate_chars(messages: Iterable[Message], *, attachment_chars: int = 0) -> int:
    """估算一批消息的**上下文体量**（字符），用于预算与日志。

    必须把 ``reasoning_content`` 算进来：DeepSeek 文档明确——
    **请求携带 ``tools`` 时，历史轮次的思维链要回传并拼接进上下文**
    （我们每轮都带工具，所以它实实在在占窗口）。
    漏算会让预算虚高：实测这条会话"正文 468k tok + 思维链 235k tok"，
    而按字符估算时只看到前者。

    附件内容块（图片/文件）在消息里只是 ``blue+attachment://<hash>`` 伪 URL，
    真实字节要到发请求时才 materialize 成 ``data:`` URL——按文本估算等于把它们
    当成零成本。``attachment_chars > 0`` 时给每个附件块记这么多**当量字符**，
    让预算能感知图片（默认 ``0`` 保持"只算文本"的旧口径）。

    :param messages: 待估算的消息。
    :param attachment_chars: 每个附件内容块折算的字符数；``0`` 表示不计入。
    :return: 估算的字符总量。
    """
    total = 0
    for message in messages:
        total += len(_message_text(message))
        total += len(message.reasoning_content or "")
        total += _attachment_count(message) * attachment_chars
        for call in message.tool_calls or []:
            total += len(json.dumps(call.function.arguments, ensure_ascii=False))
    return total


def count_attachments(messages: Iterable[Message]) -> int:
    """统计这批消息里的附件内容块数量（图片与文件各算一个）。

    :param messages: 待统计的消息。
    :return: 内容块个数；纯文本消息计 0。
    """
    return sum(_attachment_count(message) for message in messages)


def compact_history(
    messages: Iterable[Message],
    *,
    keep_messages: int = COMPACT_KEEP_MESSAGES,
    text_chars: int = COMPACT_TEXT_CHARS,
    error_chars: int = COMPACT_ERROR_CHARS,
    list_items: int = COMPACT_LIST_ITEMS,
    rules: Sequence[CompactRule] = (),
    profiles: Mapping[str, Mapping[str, FieldProfile]] | None = None,
    target_chars: int = 0,
    boundary_step: int = COMPACT_BOUNDARY_STEP,
    attachment_chars: int = 0,
) -> list[Message]:
    """返回压缩后的历史副本；尾部 ``keep_messages`` 条原样保留。

    只按「条数」保留是不够的：1700 条历史哪怕每条只剩 200 字，加起来也有几百 KB。
    因此再加一层**预算**：``target_chars > 0`` 时，如果压完仍超预算，
    就从最旧的**整步**（assistant + 它的工具结果）开始丢弃，并在原位留一句省略
    说明，直到进入预算或再没有可丢的步。

    :param rules: 按工具定制的规则（优先生效）。
    :param profiles: ``工具名 → 字段画像``，来自 :class:`SchemaLearner`。
    :param target_chars: 目标提示体量（字符）；``0`` 表示只按条数压缩。
    :param boundary_step: 投影分界的量化步长；``0`` 表示逐条移动分界。
    :param attachment_chars: 每个附件内容块折算的字符数；``0`` 表示不计入。
    :return: 新列表；入参不会被修改。
    """
    source = list(messages)
    tools = _tool_names(source)
    split = _keep_start(source, keep_messages, boundary_step)
    compacted: list[Message] = []
    for index, message in enumerate(source):
        if index >= split:
            compacted.append(message)
            continue
        tool = tools.get(message.tool_call_id or "", "")
        limits = _limits_for(tool, rules, profiles, text_chars, error_chars, list_items)
        compacted.append(_compact_message(message, tool=tool, limits=limits))
    if target_chars > 0:
        return _fit_budget(
            compacted,
            target_chars=target_chars,
            keep_from=split,
            attachment_chars=attachment_chars,
        )
    return compacted


def _split_steps(messages: Sequence[Message], start: int) -> list[tuple[int, int]]:
    """把消息切成「步」：一条 assistant 加上紧随其后的 tool 结果。

    淘汰只能按步来：把 assistant 丢掉却留下它的 tool 结果（或反过来），下游会直接
    报「tool_call 没有对应结果」，整轮对话就废了。
    """
    steps: list[tuple[int, int]] = []
    index = start
    while index < len(messages):
        end = index + 1
        if messages[index].role == MessageRole.ASSISTANT and messages[index].tool_calls:
            while end < len(messages) and messages[end].role == MessageRole.TOOL:
                end += 1
        steps.append((index, end))
        index = end
    return steps


def _fit_budget(
    messages: list[Message],
    *,
    target_chars: int,
    keep_from: int,
    attachment_chars: int,
) -> list[Message]:
    """超预算时从最旧的**整步**开始丢弃，并在原位留一句省略说明。

    为什么不是"把每条旧消息截成一行"：工具结果几乎都是 JSON，截前 120 字拿到的
    是 `{` 和第一个键名——既没信息量，又和「按字段投影」那层语义重复。丢弃整步
    更省、更干净，模型也只需要知道"这段被省略了，要细节可以重读"。

    保留：系统提示、最后一步（当前这轮的输入）；预算压到小于最后一步时也不丢它，
    只是如实承认超预算。``keep_from`` 之后是「原样保留区」：正常情况下丢弃止步于
    这里，只有预算小到保留区自己也放不下时才会动用它——此时在省略说明里如实
    写出"最近 N 条也被省略"，不让模型误以为近期上下文还在。
    """
    if target_chars <= 0 or len(messages) <= 1:
        return messages
    total = estimate_chars(messages, attachment_chars=attachment_chars)
    if total <= target_chars:
        return messages

    head = 1 if messages[0].role == MessageRole.SYSTEM else 0
    budget = max(0, target_chars - NOTES_RESERVE)
    dropped = 0
    dropped_steps = 0
    dropped_recent = 0
    resume = head
    for start, end in _split_steps(messages, head):
        if total <= budget or end >= len(messages):
            break  # 进预算了，或只剩最后一步（当前轮输入，不能丢）
        total -= estimate_chars(messages[start:end], attachment_chars=attachment_chars)
        dropped += end - start
        dropped_steps += 1
        if start >= keep_from:
            dropped_recent += end - start
        resume = end
    if not dropped:
        return messages
    note = Message.user(
        DROPPED_NOTE.format(
            steps=dropped_steps,
            messages=dropped,
            recent=RECENT_NOTE.format(count=dropped_recent) if dropped_recent else "",
        )
    )
    return [*messages[:head], note, *messages[resume:]]


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


def _keep_start(messages: list[Message], keep_messages: int, boundary_step: int) -> int:
    """算出「从哪条开始原样保留」：先量化分界，再对齐 tool 配对边界。

    量化只对已经在步长之外的区间生效（``start < boundary_step`` 时原样保留），
    否则短会话会被整体取消压缩。对齐 tool 配对只会让分界继续**前移**，因此
    「保留条数不少于 ``keep_messages``」与「分界稳定」两个性质同时成立。
    """
    if keep_messages <= 0:
        return len(messages)
    start = max(0, len(messages) - keep_messages)
    if boundary_step > 0 and start >= boundary_step:
        start -= start % boundary_step
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


def _attachment_count(message: Message) -> int:
    """数一条消息里的附件内容块（图片或文件）。"""
    content = message.content
    if isinstance(content, str):
        return 0
    count = 0
    for item in content or []:
        if item.image_url is not None or item.file is not None:
            count += 1
    return count
