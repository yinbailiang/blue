"""MCP 的 ``inputSchema`` → ``infinity_agent`` 工具参数。

转换本体复用 ``infinity_agent.tools.introspectionist.convert``（``resolve_refs``
内联 ``$defs``、``prop_from_schema`` 逐属性降维）。本模块只补它不做、且实测会
**崩溃**或**静默失真**的那部分前置归一化：

- ``definitions`` + ``#/definitions/``：``resolve_refs`` 只认 ``#/$defs/``，
  未命中时 ``prop_from_schema`` 会把该参数静默退化成 ``string``；
- ``{"type": "array"}`` / ``prefixItems`` / 元组式 ``items`` / ``{"type": "null"}``：
  ``ParameterProperty`` 校验直接抛错；
- ``allOf`` / ``const`` / ``type: [...]`` / ``format`` 等关键字：库不处理，
  会静默失真。

降级说明以文本形式回传，由调用方写进工具描述，让模型与人都看得见。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from infinity_agent import ParameterProperty, ToolParameters

# ``infinity_agent.tools.introspectionist`` 是没有 ``__init__.py`` 的隐式命名空间
# 子包，pyright 因此认为它缺失类型标记；源码就在旁边，类型可直接推断。
from infinity_agent.tools.introspectionist.convert import (  # pyright: ignore[reportMissingTypeStubs]
    prop_from_schema,
    resolve_refs,
)

__all__ = ["MAX_NOTES", "SchemaConversion", "convert_input_schema"]

logger = logging.getLogger(__name__)

MAX_NOTES = 6
"""工具描述里最多列出的降级说明条数，其余折叠为计数。"""

FALLBACK_DESCRIPTION = "参数 schema 无法表达，已降级为字符串"
"""单个参数转换失败时的占位描述。"""

_UNSUPPORTED_KEYWORDS = (
    "minItems",
    "maxItems",
    "uniqueItems",
    "multipleOf",
    "not",
    "if",
    "then",
    "else",
    "patternProperties",
    "dependentRequired",
    "dependentSchemas",
    "propertyNames",
    "contains",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentMediaType",
    "contentEncoding",
    "$anchor",
    "$schema",
    "$id",
)
"""``ParameterProperty`` 表达不了的关键字：丢弃并记录。"""


@dataclass(frozen=True)
class SchemaConversion:
    """一次 schema 转换的结果与降级记录。"""

    parameters: ToolParameters
    notes: tuple[str, ...] = ()

    def describe(self, description: str) -> str:
        """把降级说明追加到工具描述尾部（无降级时原样返回）。"""
        if not self.notes:
            return description
        shown = "；".join(self.notes[:MAX_NOTES])
        suffix = (
            f"（另有 {len(self.notes) - MAX_NOTES} 条未列出）"
            if len(self.notes) > MAX_NOTES
            else ""
        )
        return f"{description}\n\n[blue] 参数 schema 已降级：{shown}{suffix}"


def convert_input_schema(schema: Mapping[str, object] | None) -> SchemaConversion:
    """把 MCP 的 ``inputSchema`` 转成工具参数 schema。

    任何降级都不抛异常：崩溃类问题在此就地修好，失真类问题记进
    :attr:`SchemaConversion.notes`，由调用方告警并写进工具描述。

    :param schema: MCP 工具声明的 ``inputSchema``；``None`` 或空对象表示无参数。
    :return: 转换后的参数 schema 与降级说明。
    """
    notes: list[str] = []
    root = _normalize_root(_pre_normalize(_as_dict(schema), notes, path=""), notes)
    resolved = _as_dict(resolve_refs(root))

    properties: dict[str, ParameterProperty] = {}
    raw_properties = _as_dict(resolved.get("properties"))
    for name, node in raw_properties.items():
        properties[name] = _convert_property(name, node, notes)

    declared_required = _as_str_list(resolved.get("required"))
    required = [name for name in declared_required if name in properties]
    dropped = [name for name in declared_required if name not in properties]
    if dropped:
        notes.append(f"`required` 中的 {dropped} 未出现在 `properties` 里，已忽略")

    return SchemaConversion(
        parameters=ToolParameters(
            type="object", properties=properties, required=required
        ),
        notes=tuple(notes),
    )


def _convert_property(name: str, node: object, notes: list[str]) -> ParameterProperty:
    """转换单个属性；单个参数失败不拖垮整个工具。"""
    raw = _as_dict(node)
    try:
        return prop_from_schema(raw)
    except Exception as error:
        message = str(error).splitlines()[0]
        notes.append(f"`{name}`: {FALLBACK_DESCRIPTION}（{message}）")
        logger.warning("参数 %s 的 schema 转换失败，已降级为字符串：%s", name, message)
        description = _as_str(raw.get("description")).strip()
        return ParameterProperty(
            type="string",
            description=f"{description}（{FALLBACK_DESCRIPTION}）".strip(),
        )


def _normalize_root(root: dict[str, object], notes: list[str]) -> dict[str, object]:
    """保证顶层是 object schema：非 object 的整棵 schema 包装成单项参数。"""
    raw_type = root.get("type")
    if raw_type is None or raw_type == "object":
        return {"type": "object", **root}
    # 顶层的 array / string 等：包成唯一参数 input，保证工具仍可被调用。
    notes.append(
        f"`inputSchema` 顶层类型为 {raw_type!r}，已包装成 object 的单项参数 `input`"
    )
    return {
        "type": "object",
        "properties": {"input": root},
        "required": ["input"],
    }


def _pre_normalize(
    node: dict[str, object], notes: list[str], *, path: str, in_union: bool = False
) -> dict[str, object]:
    """递归归一化一棵 JSON Schema 节点（不自引用，返回新 dict）。"""
    normalized: dict[str, object] = dict(node)
    _normalize_definitions(normalized, notes, path=path)
    _normalize_ref(normalized, notes, path=path)
    _normalize_const(normalized, notes, path=path)
    _normalize_composition(normalized, notes, path=path)
    _normalize_type(normalized, notes, path=path, in_union=in_union)
    _normalize_array(normalized, notes, path=path)
    _normalize_format(normalized, notes, path=path)
    _drop_unsupported(normalized, notes, path=path)
    _recurse(normalized, notes, path=path)
    return normalized


def _normalize_definitions(
    node: dict[str, object], notes: list[str], *, path: str
) -> None:
    """把 draft-07 的 ``definitions`` 并入 ``$defs``（zod v3 默认输出前者）。"""
    if "definitions" not in node:
        return
    legacy = _as_dict(node.pop("definitions"))
    current = _as_dict(node.get("$defs"))
    node["$defs"] = {**legacy, **current}
    notes.append(
        f"`{path or '.'}`: `definitions` 已并入 `$defs`（否则 `$ref` 不会被解析）"
    )


def _normalize_ref(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """把 ``#/definitions/...`` 引用改写成 ``#/$defs/...``。"""
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return
    prefix = "#/definitions/"
    if ref.startswith(prefix):
        node["$ref"] = f"#/$defs/{ref[len(prefix) :]}"
        notes.append(f"`{path or '.'}`: `$ref {ref}` 已改写为 `#/$defs/` 形式")


def _normalize_const(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """``const`` 转成单值 ``enum``（``ParameterProperty`` 没有 ``const``）。"""
    if "const" not in node:
        return
    value = node.pop("const")
    if "enum" not in node:
        node["enum"] = [value]
    notes.append(f"`{path or '.'}`: `const` 已转成单值 `enum`")


def _normalize_composition(
    node: dict[str, object], notes: list[str], *, path: str
) -> None:
    """处理 ``oneOf``（降级为 ``anyOf``）与 ``allOf``（合并或丢弃）。"""
    if "oneOf" in node:
        node["anyOf"] = node.pop("oneOf")
        notes.append(
            f"`{path or '.'}`: `oneOf` 降级为 `anyOf`（「恰好一个」变「其中一个」）"
        )
    if "allOf" not in node:
        return
    branches = _as_list(node.pop("allOf"))
    merged = _merge_all_of(branches)
    if merged is None:
        notes.append(f"`{path or '.'}`: `allOf` 无法合并，已丢弃")
        return
    for key, value in merged.items():
        node[key] = value
    notes.append(f"`{path or '.'}`: `allOf` 已合并进父级 schema")


def _merge_all_of(branches: list[object]) -> dict[str, object] | None:
    """合并 ``allOf`` 分支；无法安全合并时返回 ``None``。"""
    dicts = [_as_dict(branch) for branch in branches]
    dicts = [branch for branch in dicts if branch]
    if not dicts:
        return None
    if len(dicts) == 1:
        return dicts[0]
    for branch in dicts:
        branch_type = _as_str(branch.get("type"))
        if branch_type not in ("", "object") or "properties" not in branch:
            return None
    properties: dict[str, object] = {}
    required: set[str] = set()
    for branch in dicts:
        properties.update(_as_dict(branch.get("properties")))
        required.update(_as_str_list(branch.get("required")))
    return {"type": "object", "properties": properties, "required": sorted(required)}


def _normalize_type(
    node: dict[str, object], notes: list[str], *, path: str, in_union: bool = False
) -> None:
    """处理联合类型数组与纯 ``null`` 类型。"""
    raw_type = node.get("type")
    if isinstance(raw_type, list):
        declared = cast(list[object], raw_type)
        types = [item for item in declared if isinstance(item, str)]
        node.pop("type", None)
        node["anyOf"] = [{"type": item} for item in types]
        notes.append(f"`{path or '.'}`: 联合类型 {types} 已转成 `anyOf`")
        return
    if raw_type == "null":
        if in_union:
            # anyOf 里的 null 分支是合法且有用的：留给 prop_from_schema 转成 nullable
            return
        node["type"] = "string"
        notes.append(f"`{path or '.'}`: 纯 `null` 类型已降级为 `string`")


def _normalize_array(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """修补 ``array`` 的 ``items``：缺失、元组式或 ``prefixItems`` 降级为任意元素。"""
    prefix = node.pop("prefixItems", None)
    if prefix is not None:
        notes.append(f"`{path or '.'}`: `prefixItems`（元组）已降级为任意元素")
    if node.get("type") != "array":
        return
    items = node.get("items")
    if isinstance(items, list):
        node["items"] = {}
        notes.append(f"`{path or '.'}`: 元组式 `items` 已降级为任意元素")
        return
    if items is None:
        node["items"] = {}
        notes.append(f"`{path or '.'}`: `array` 缺少 `items`，已补任意元素")


def _normalize_format(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """``format`` 不受支持：搬进 ``description`` 保留语义。"""
    fmt = node.get("format")
    if not isinstance(fmt, str):
        return
    node.pop("format", None)
    description = _as_str(node.get("description")).strip()
    suffix = f"（format: {fmt}）"
    node["description"] = f"{description} {suffix}".strip()
    notes.append(f"`{path or '.'}`: `format={fmt}` 不受支持，已移入描述")


def _drop_unsupported(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """丢弃 ``ParameterProperty`` 表达不了的关键字。"""
    for keyword in _UNSUPPORTED_KEYWORDS:
        if keyword in node:
            node.pop(keyword, None)
            notes.append(f"`{path or '.'}`: `{keyword}` 不受支持，已丢弃")


def _recurse(node: dict[str, object], notes: list[str], *, path: str) -> None:
    """递归归一化子节点：``properties`` / ``items`` / ``additionalProperties``。"""
    properties = node.get("properties")
    if isinstance(properties, dict):
        children = cast(dict[str, object], properties)
        node["properties"] = {
            name: _pre_normalize(
                _as_dict(child), notes, path=f"{path}.{name}" if path else name
            )
            for name, child in children.items()
        }
    for keyword in ("items", "additionalProperties"):
        child = node.get(keyword)
        if isinstance(child, dict):
            node[keyword] = _pre_normalize(
                cast(dict[str, object], child), notes, path=f"{path}[]"
            )
    variants = node.get("anyOf")
    if isinstance(variants, list):
        node["anyOf"] = [
            _pre_normalize(_as_dict(variant), notes, path=path, in_union=True)
            for variant in cast(list[object], variants)
        ]


def _as_dict(value: object) -> dict[str, object]:
    """把 JSON 对象视图收敛为 ``dict[str, object]``；其它类型返回空字典。"""
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): item for key, item in mapping.items()}
    return {}


def _as_list(value: object) -> list[object]:
    """把 JSON 数组视图收敛为 ``list[object]``。"""
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _as_str(value: object) -> str:
    """取字符串值；非字符串返回空串。"""
    return value if isinstance(value, str) else ""


def _as_str_list(value: object) -> list[str]:
    """取字符串数组。"""
    return [item for item in _as_list(value) if isinstance(item, str)]
