"""内建的历史回取工具：压缩把旧对话压掉了，模型用这两个工具把原文取回来。

为什么做在 agent 里而不是一个独立工具服务：本轮的历史本来就在 agent 手里
（``session.get`` 的快照 + 解码结果），把它再绕一趟总线去问会话服务，既慢又
要重复序列化几 MB 的 JSON。内建工具直接在内存里查，零额外往返。

两个工具：

- ``history_search``：在**本轮可见的全部历史**（压缩之前的）里按子串搜索，
  返回命中消息的 id、角色、在链上的序号，以及命中处的上下文片段；
- ``history_read``：按 id 读回完整原文（单条有字符上限，避免一条 35 KB 的
  工具结果再次把上下文塞爆）。

配合压缩的省略说明使用：提示里出现「早期历史已省略」时，先用 search 找到位置，
再 read 取回原文——这样上下文预算与可回溯性就不冲突了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, cast

from infinity_agent import (
    Message,
    MessageRole,
    ToolDefinition,
    ToolFunction,
    ToolParameters,
)

__all__ = [
    "HISTORY_READ_TOOL",
    "HISTORY_SEARCH_TOOL",
    "HISTORY_TOOLS",
    "HISTORY_TOOL_NAMES",
    "SNIPPET_CHARS",
    "HistoryEntry",
    "HistoryIndex",
]

SNIPPET_CHARS: Final = 160
"""搜索结果里命中处的上下文片段长度。"""

MATCH_MAX_CHARS: Final = 4000
"""``history_read`` 单条返回的字符上限（超出部分给出省略标记与真实长度）。"""

SEARCH_LIMIT_MAX: Final = 50
"""一次搜索最多返回多少条命中。"""

ROLE_ALIASES: Final[dict[str, MessageRole]] = {
    "system": MessageRole.SYSTEM,
    "user": MessageRole.USER,
    "assistant": MessageRole.ASSISTANT,
    "tool": MessageRole.TOOL,
}

HISTORY_SEARCH_TOOL: Final = ToolDefinition(
    function=ToolFunction(
        name="history_search",
        description=(
            "在本会话的历史里按子串搜索（大小写不敏感）。上下文里出现"
            "「早期历史已省略」或某条消息被省略/截断时，用它定位，再用"
            " history_read 取回原文。"
        ),
        parameters=ToolParameters.model_validate(
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "要搜索的文本（子串匹配，可为命令名、报错、关键词）"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"最多返回多少条命中，1~{SEARCH_LIMIT_MAX}，默认 5"
                        ),
                    },
                    "roles": {
                        "type": "string",
                        "description": (
                            "只看这些角色，逗号分隔；默认 user,assistant,tool"
                            "（不含 system）"
                        ),
                    },
                },
                "required": ["query"],
            }
        ),
    )
)
"""``history_search`` 的工具声明。"""

HISTORY_READ_TOOL: Final = ToolDefinition(
    function=ToolFunction(
        name="history_read",
        description=(
            "按 id 读回历史消息的完整原文（id 来自 history_search 或消息上的标注）。"
            "单条超长会被截断并标注真实长度。"
        ),
        parameters=ToolParameters.model_validate(
            {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "要读取的消息 id 列表（1~20 个）",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": f"每条返回的字符上限，默认 {MATCH_MAX_CHARS}",
                    },
                },
                "required": ["ids"],
            }
        ),
    )
)
"""``history_read`` 的工具声明。"""

HISTORY_TOOLS: Final[tuple[ToolDefinition, ...]] = (
    HISTORY_SEARCH_TOOL,
    HISTORY_READ_TOOL,
)
"""内建历史工具的声明元组（直接拼进模型的工具目录）。"""

HISTORY_TOOL_NAMES: Final = frozenset(
    {
        HISTORY_SEARCH_TOOL.function.name,
        HISTORY_READ_TOOL.function.name,
    }
)
"""内建历史工具的名字集合（agent 据此把调用拦在本地，不走总线）。"""


@dataclass(frozen=True)
class HistoryEntry:
    """历史里的一条消息：树节点 id + 解码后的内容。"""

    id: int
    message: Message

    @property
    def role(self) -> str:
        """角色名（小写）。"""
        return _role_name(self.message)

    @property
    def text(self) -> str:
        """消息里可读的纯文本（多模态只取文本片段）。"""
        return message_text(self.message)


class HistoryIndex:
    """本轮可见历史的只读检索视图。

    ``entries`` 是**压缩之前**的完整历史（按链上顺序），所以模型能取回任何被
    省略或截断的内容。
    """

    def __init__(self, entries: list[HistoryEntry]) -> None:
        self._entries = tuple(entries)
        self._by_id = {entry.id: entry for entry in entries}

    def __len__(self) -> int:
        """历史条数。"""
        return len(self._entries)

    def get(self, node_id: int) -> HistoryEntry | None:
        """按 id 取一条（不存在返回 None）。"""
        return self._by_id.get(node_id)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        roles: set[MessageRole] | None = None,
    ) -> dict[str, Any]:
        """子串搜索，返回可直接回灌给模型的 JSON 结构。"""
        needle = query.strip().lower()
        if not needle:
            return {"error": "query 不能为空", "hits": []}
        wanted = roles or {MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL}
        hits: list[dict[str, Any]] = []
        total = 0
        for index, entry in enumerate(self._entries):
            if entry.message.role not in wanted:
                continue
            text = entry.text
            position = text.lower().find(needle)
            if position < 0:
                continue
            total += 1
            if len(hits) >= max(1, min(limit, SEARCH_LIMIT_MAX)):
                continue
            hits.append(
                {
                    "id": entry.id,
                    "role": entry.role,
                    "index": index,
                    "chars": len(text),
                    "snippet": _snippet(text, position, len(needle)),
                }
            )
        return {"query": query, "total": total, "hits": hits}

    def read(
        self, ids: list[int], *, max_chars: int = MATCH_MAX_CHARS
    ) -> dict[str, Any]:
        """按 id 读原文；缺失的 id 单独列出来，别让模型以为拿到的是全部。"""
        limit = max(200, min(max_chars, 200_000))
        messages: list[dict[str, Any]] = []
        missing: list[int] = []
        for node_id in ids[:20]:
            entry = self._by_id.get(node_id)
            if entry is None:
                missing.append(node_id)
                continue
            text = entry.text
            item: dict[str, Any] = {
                "id": entry.id,
                "role": entry.role,
                "chars": len(text),
            }
            if len(text) > limit:
                item["content"] = f"{text[:limit]}…[{len(text)} chars total]"
                item["truncated"] = True
            else:
                item["content"] = text
            messages.append(item)
        payload: dict[str, Any] = {"messages": messages}
        if missing:
            payload["missing"] = missing
        return payload

    def call(self, name: str, arguments: object = None) -> str:
        """执行一次内建工具调用，返回要回灌给模型的文本（错误也写成文本）。

        ``arguments`` 既可能是模型给的 JSON 字符串，也可能是 SDK 已解析好的 dict
        （``ToolCall.function.arguments`` 就是 dict），两种都收。
        """
        if isinstance(arguments, str):
            try:
                parsed: object = json.loads(arguments or "{}")
            except json.JSONDecodeError as error:
                return json.dumps(
                    {"error": f"参数不是合法 JSON：{error}"}, ensure_ascii=False
                )
        else:
            parsed = arguments if arguments is not None else {}
        if not isinstance(parsed, dict):
            return json.dumps({"error": "参数必须是 JSON 对象"}, ensure_ascii=False)
        mapping = cast("dict[object, object]", parsed)
        args: dict[str, object] = {str(key): value for key, value in mapping.items()}
        if name == "history_search":
            query = str(args.get("query") or "")
            limit = _as_int(args.get("limit"), 5)
            roles = _parse_roles(args.get("roles"))
            return json.dumps(
                self.search(query, limit=limit, roles=roles), ensure_ascii=False
            )
        if name == "history_read":
            ids = _as_ids(args.get("ids"))
            if not ids:
                return json.dumps({"error": "ids 需要至少一个整数"}, ensure_ascii=False)
            max_chars = _as_int(args.get("max_chars"), MATCH_MAX_CHARS)
            return json.dumps(self.read(ids, max_chars=max_chars), ensure_ascii=False)
        return json.dumps({"error": f"未知的历史工具：{name}"}, ensure_ascii=False)


def _role_name(message: Message) -> str:
    """角色名（小写），兼容枚举与字符串两种形态。"""
    role = message.role
    value = getattr(role, "value", role)
    return str(value).lower()


def message_text(message: Message) -> str:
    """消息里可读的纯文本：多模态只拼文本片段，图片用占位符标注。"""
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        kind = getattr(part, "type", "")
        if kind == "text":
            parts.append(str(getattr(part, "text", "")))
        elif kind in {"image_url", "image"}:
            parts.append("[图片]")
        else:
            parts.append(f"[{kind or '非文本'}]")
    return "\n".join(part for part in parts if part)


def _snippet(text: str, position: int, length: int) -> str:
    """命中处前后各留一点上下文，压平空白后给出单行片段。"""
    half = max(20, (SNIPPET_CHARS - length) // 2)
    start = max(0, position - half)
    end = min(len(text), position + length + half)
    piece = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{piece}{suffix}"


def _parse_roles(raw: object) -> set[MessageRole] | None:
    """把 ``"user,assistant"`` 这样的串解析成角色集合。"""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return None
    roles: set[MessageRole] = set()
    for piece in raw.split(","):
        name = piece.strip().lower()
        if name in ROLE_ALIASES:
            roles.add(ROLE_ALIASES[name])
    return roles or None


def _as_int(raw: object, default: int) -> int:
    """宽容地把参数转成整数（模型偶尔会传字符串）。"""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return default


def _as_ids(raw: object) -> list[int]:
    """把 ``ids`` 参数解析成整数列表（容许单个整数或字符串数组）。"""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return [raw]
    if not isinstance(raw, list):
        return []
    items = cast("list[object]", raw)
    return [_as_int(item, -1) for item in items if _as_int(item, -1) > 0]
