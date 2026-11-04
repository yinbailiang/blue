"""从历史工具结果归纳字段画像，给压缩器提供「这个字段有多长」的先验。

为什么不用静态 schema：blue 原生工具有 Pydantic 返回类型，但 **MCP 桥接进来的
工具没有**（只能拿到 ``inputSchema``），而 ``ToolDefinition`` 也不允许附加自定义
字段。所以这里直接从**历史结果**归纳——任何工具、任何语言写的 server 都适用：

- 同一工具多次返回同一字段 → 取观测到的最大长度与类型；
- 观测到长字符串的字段，压缩时按「大文本」截断；
- 观测到始终是 bool / 小整数 / 短路经的字段，压缩时强制保留（状态类信息）。

归纳只用于**判断策略**，不改数据；观测样本不足时回退到字段名启发式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

__all__ = ["FieldProfile", "SchemaLearner"]

LONG_TEXT = 400
"""观测长度超过它才算「长文本」（与压缩器的 SHORT_TEXT 对齐）。"""

MAX_FIELDS = 64
"""单个工具最多记录这么多字段，避免异常数据撑爆。"""


@dataclass
class FieldProfile:
    """一个字段的观测画像。"""

    samples: int = 0
    max_length: int = 0
    seen_text: bool = False
    """观测到需要截断的长文本。"""

    seen_scalar: bool = False
    """观测到短标量（bool / 数字 / 短字符串 / 路径）。"""

    @property
    def is_text(self) -> bool:
        """是否应按大文本处理。"""
        return self.seen_text

    @property
    def keep(self) -> bool:
        """是否属于「状态类」信息，应始终保留。"""
        return self.seen_scalar and not self.seen_text


@dataclass
class _ToolProfile:
    fields: dict[str, FieldProfile] = field(default_factory=dict[str, FieldProfile])


class SchemaLearner:
    """按工具名收集字段画像。"""

    def __init__(self) -> None:
        self._tools: dict[str, _ToolProfile] = {}

    def observe(self, tool: str, payload: object) -> None:
        """喂一次工具结果（已解析的 JSON 值）。"""
        if not tool:
            return
        profile = self._tools.setdefault(tool, _ToolProfile())
        self._absorb(profile, payload)

    def observe_text(self, tool: str, body: str) -> None:
        """喂一条原始结果文本（能解析成 JSON 才归纳）。"""
        try:
            payload: object = json.loads(body)
        except json.JSONDecodeError:
            return
        self.observe(tool, payload)

    def profiles(self, tool: str) -> dict[str, FieldProfile]:
        """取某个工具的字段画像（未知工具返回空）。"""
        found = self._tools.get(tool)
        return dict(found.fields) if found else {}

    def tools(self) -> list[str]:
        """已观测到的工具名。"""
        return sorted(self._tools)

    def _absorb(self, profile: _ToolProfile, value: object) -> None:
        if not isinstance(value, dict):
            return
        if len(profile.fields) > MAX_FIELDS:
            return
        for key, item in cast(dict[str, object], value).items():
            name = str(key)
            field_profile = profile.fields.setdefault(name, FieldProfile())
            _record(field_profile, item)


def _record(profile: FieldProfile, value: object) -> None:
    """记录一个字段取值。"""
    profile.samples += 1
    if isinstance(value, str):
        profile.max_length = max(profile.max_length, len(value))
        if len(value) > LONG_TEXT:
            profile.seen_text = True
        else:
            profile.seen_scalar = True
        return
    if isinstance(value, bool | int | float) or value is None:
        profile.seen_scalar = True
        return
    # 列表/嵌套对象：不参与「是否长文本」的判断，但记录样本
    rendered = len(json.dumps(value, ensure_ascii=False))
    profile.max_length = max(profile.max_length, rendered)
