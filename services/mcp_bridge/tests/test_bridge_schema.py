"""schema 转换测试：覆盖「复用 infinity_agent 清洗逻辑」的每一条实测 gap。

用例分三类：
- 正常映射：对象、基础类型、enum、嵌套、数组；
- **崩溃类**（不修就会抛 ``ValidationError``）：``array`` 无 ``items``、
  ``prefixItems``、元组式 ``items``、``type: null``；
- **失真类**（不修就静默变成 string）：``definitions``/``$ref``、``allOf``、
  ``const``、``type: [...]``、``format``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from blue_service_mcp_bridge.schema import SchemaConversion, convert_input_schema
from infinity_agent import ParameterProperty, ToolParameters


@dataclass(frozen=True)
class _View:
    """参数 schema 的强类型投影（``ToolParameters`` 的字段在类型上是可空的）。"""

    type: str
    properties: dict[str, ParameterProperty]
    required: list[str]


def _view(parameters: ToolParameters) -> _View:
    """把 ``ToolParameters`` 投影成非空视图。"""
    return _View(
        type=parameters.type,
        properties=parameters.properties or {},
        required=list(parameters.required),
    )


def _convert(schema: dict[str, Any]) -> SchemaConversion:
    """转换并返回完整结果。"""
    return convert_input_schema(schema)


def _params(schema: dict[str, Any]) -> _View:
    """只取参数 schema。"""
    return _view(_convert(schema).parameters)


def _properties(conversion: SchemaConversion) -> dict[str, ParameterProperty]:
    """取转换结果里的 properties（保证非空）。"""
    return _view(conversion.parameters).properties


def _required(conversion: SchemaConversion) -> list[str]:
    """取转换结果里的 required。"""
    return _view(conversion.parameters).required


def test_object_schema_maps_types_and_required() -> None:
    """基础类型、enum、必填列表 1:1 映射。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "文本"},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean", "default": False},
                "kind": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["text", "kind"],
        }
    )
    params = conversion.parameters
    assert params.type == "object"
    assert params.required == ["text", "kind"]
    assert params.properties["text"].type == "string"
    assert params.properties["text"].description == "文本"
    assert params.properties["count"].minimum == 1
    assert params.properties["count"].maximum == 10
    assert params.properties["flag"].default is False
    assert params.properties["kind"].enum == ["a", "b"]
    assert conversion.notes == ()


def test_nested_object_and_array_items() -> None:
    """嵌套对象与 ``items`` 递归转换。"""
    params = _params(
        {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
    )
    assert params.properties["filter"].type == "object"
    assert (params.properties["filter"].properties or {})["pattern"].type == "string"
    assert params.properties["filter"].required == ["pattern"]
    assert params.properties["tags"].type == "array"
    assert params.properties["tags"].items is not None
    assert params.properties["tags"].items.type == "string"


def test_free_form_object_gets_additional_properties() -> None:
    """裸 ``object`` 允许任意键值。"""
    params = _params({"type": "object", "properties": {"meta": {"type": "object"}}})
    assert params.properties["meta"].additionalProperties is True


def test_definitions_are_merged_into_defs() -> None:
    """zod v3 的 ``definitions`` + ``#/definitions/`` 必须被解析而不是静默变 string。"""
    conversion = _convert(
        {
            "type": "object",
            "definitions": {
                "Filter": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                }
            },
            "properties": {"filter": {"$ref": "#/definitions/Filter"}},
            "required": ["filter"],
        }
    )
    prop = _properties(conversion)["filter"]
    assert prop.type == "object"
    assert (prop.properties or {})["pattern"].type == "string"
    assert any("definitions" in note for note in conversion.notes)
    assert any("$ref" in note for note in conversion.notes)


def test_circular_ref_is_truncated_not_hanging() -> None:
    """循环引用被截断成 object，转换不挂死。"""
    conversion = _convert(
        {
            "type": "object",
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        }
    )
    assert _properties(conversion)["root"].type == "object"


def test_array_without_items_does_not_crash() -> None:
    """``{"type": "array"}`` 是合法 schema，但库会抛错：必须补 ``items``。"""
    conversion = _convert(
        {"type": "object", "properties": {"values": {"type": "array"}}}
    )
    prop = _properties(conversion)["values"]
    assert prop.type == "array"
    assert prop.items is not None
    assert any("items" in note for note in conversion.notes)


def test_prefix_items_does_not_crash() -> None:
    """``prefixItems``（元组）降级为任意元素数组。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "pair": {
                    "type": "array",
                    "prefixItems": [{"type": "string"}, {"type": "integer"}],
                }
            },
        }
    )
    assert _properties(conversion)["pair"].type == "array"
    assert any("prefixItems" in note for note in conversion.notes)


def test_tuple_style_items_does_not_crash() -> None:
    """draft-07 的 ``items: [...]`` 降级为任意元素。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "pair": {"type": "array", "items": [{"type": "string"}]},
            },
        }
    )
    assert _properties(conversion)["pair"].type == "array"


def test_null_only_type_does_not_crash() -> None:
    """纯 ``null`` 类型降级为 string 并记录。"""
    conversion = _convert(
        {"type": "object", "properties": {"nothing": {"type": "null"}}}
    )
    assert _properties(conversion)["nothing"].type == "string"
    assert any("null" in note for note in conversion.notes)


def test_union_type_with_null_becomes_nullable() -> None:
    """``type: ["string", "null"]`` 提升为主类型并标记可空。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {"name": {"type": ["string", "null"]}},
        }
    )
    prop = _properties(conversion)["name"]
    assert prop.type == "string"
    assert prop.nullable is True
    assert any("联合类型" in note for note in conversion.notes)


def test_union_type_without_null_becomes_anyof() -> None:
    """``type: ["string", "integer"]`` 保留 ``anyOf`` 两支。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {"value": {"type": ["string", "integer"]}},
        }
    )
    prop = _properties(conversion)["value"]
    assert prop.anyOf is not None
    assert len(prop.anyOf) == 2


def test_const_becomes_single_enum() -> None:
    """``const`` 转成单值 ``enum``。"""
    conversion = _convert({"type": "object", "properties": {"kind": {"const": "zip"}}})
    assert _properties(conversion)["kind"].enum == ["zip"]
    assert any("const" in note for note in conversion.notes)


def test_one_of_downgrades_to_any_of() -> None:
    """``oneOf`` 降级为 ``anyOf`` 并明确记录语义变化。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            },
        }
    )
    prop = _properties(conversion)["value"]
    assert prop.anyOf is not None
    assert any("oneOf" in note and "anyOf" in note for note in conversion.notes)


def test_all_of_single_branch_is_inlined() -> None:
    """单分支 ``allOf`` 直接内联。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "name": {"allOf": [{"type": "string", "minLength": 1}]},
            },
        }
    )
    prop = _properties(conversion)["name"]
    assert prop.type == "string"
    assert prop.minLength == 1


def test_all_of_object_branches_are_merged() -> None:
    """多分支对象 ``allOf`` 合并 properties 与 required。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "filter": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "required": ["a"],
                        },
                        {
                            "type": "object",
                            "properties": {"b": {"type": "integer"}},
                            "required": ["b"],
                        },
                    ]
                }
            },
        }
    )
    prop = _properties(conversion)["filter"]
    assert prop.type == "object"
    assert set(prop.properties or {}) == {"a", "b"}
    assert prop.required == ["a", "b"]


def test_all_of_unmergeable_is_dropped_with_note() -> None:
    """无法合并的 ``allOf`` 丢弃但留痕，不静默变 string 也不崩。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "weird": {"allOf": [{"type": "string"}, {"type": "integer"}]}
            },
        }
    )
    assert _properties(conversion)["weird"] is not None
    assert any("allOf" in note for note in conversion.notes)


def test_format_moves_into_description() -> None:
    """``format`` 不受支持：搬进描述保留语义。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "at": {"type": "string", "format": "date-time", "description": "时间"},
            },
        }
    )
    prop = _properties(conversion)["at"]
    assert prop.type == "string"
    assert prop.description is not None
    assert "date-time" in prop.description
    assert "时间" in prop.description


def test_unsupported_keywords_are_dropped_with_notes() -> None:
    """``minItems`` 等关键字丢弃并留痕。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            },
        }
    )
    assert any("minItems" in note for note in conversion.notes)


def test_required_entries_missing_from_properties_are_dropped() -> None:
    """``required`` 指向不存在的属性时过滤掉，避免无效 schema。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "ghost"],
        }
    )
    assert _required(conversion) == ["a"]
    assert any("ghost" in note for note in conversion.notes)


def test_non_object_root_is_wrapped_as_single_input() -> None:
    """顶层不是 object 时包成唯一参数 ``input``，保证工具仍可调用。"""
    conversion = _convert({"type": "string", "description": "整段文本"})
    assert set(_properties(conversion)) == {"input"}
    assert _required(conversion) == ["input"]
    assert any("顶层类型" in note for note in conversion.notes)


def test_empty_or_missing_schema_yields_no_parameters() -> None:
    """无参数工具：空 properties，且不抛异常。"""
    for schema in ({}, None, {"type": "object"}):
        conversion = convert_input_schema(schema)
        assert _properties(conversion) == {}
        assert _required(conversion) == []
        assert conversion.notes == ()


def test_malformed_property_degrades_to_string_instead_of_crashing() -> None:
    """单个属性转换失败只降级该属性，其它参数不受影响。"""
    conversion = _convert(
        {
            "type": "object",
            "properties": {
                "broken": {"type": "object", "properties": "not-a-dict"},
                "ok": {"type": "string"},
            },
        }
    )
    assert _properties(conversion)["ok"].type == "string"
    assert _properties(conversion)["broken"].type in {"object", "string"}


def test_describe_appends_notes_only_when_needed() -> None:
    """无降级时描述原样返回；有降级时追加可见说明。"""
    clean = _convert({"type": "object", "properties": {"a": {"type": "string"}}})
    assert clean.describe("原始描述") == "原始描述"

    dirty = _convert({"type": "object", "properties": {"a": {"type": "array"}}})
    described = dirty.describe("原始描述")
    assert described.startswith("原始描述")
    assert "[blue] 参数 schema 已降级" in described


def test_describe_folds_many_notes() -> None:
    """降级条目过多时只列前几条并给出总数。"""
    properties = {
        f"p{index}": {"type": "string", "format": "uuid"} for index in range(10)
    }
    conversion = _convert({"type": "object", "properties": properties})
    described = conversion.describe("描述")
    assert "另有" in described


@pytest.mark.parametrize(
    "path_style",
    ["definitions", "$defs"],
)
def test_both_ref_styles_resolve(path_style: str) -> None:
    """``definitions`` 与 ``$defs`` 两种写法都能解析出对象。"""
    conversion = _convert(
        {
            "type": "object",
            path_style: {
                "Inner": {"type": "object", "properties": {"x": {"type": "string"}}}
            },
            "properties": {"inner": {"$ref": f"#/{path_style}/Inner"}},
        }
    )
    assert _properties(conversion)["inner"].type == "object"
