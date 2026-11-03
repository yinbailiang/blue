"""反向 schema 转换测试：``ToolParameters`` → MCP 的 JSON Schema。

除逐字段断言外，还用 ``services/mcp_bridge`` 的正向转换做一次往返，确保两个方向
对同一份 schema 的理解一致。
"""

from __future__ import annotations

from typing import Any

from blue_service_mcp_bridge.schema import convert_input_schema
from blue_service_mcp_server.schema import property_to_schema, to_json_schema
from infinity_agent import ParameterProperty, ToolParameters


def test_empty_parameters_yield_empty_object_schema() -> None:
    """无参数工具：object + 空 properties，且不写 required。"""
    schema = to_json_schema(ToolParameters(type="object"))
    assert schema == {"type": "object", "properties": {}}


def test_scalar_types_and_description_round_trip() -> None:
    """基础类型、描述与必填列表原样映射。"""
    parameters = ToolParameters(
        type="object",
        properties={
            "location": ParameterProperty(type="string", description="城市或地名"),
            "days": ParameterProperty(type="integer", minimum=1, maximum=3),
            "flag": ParameterProperty(type="boolean", default=False),
        },
        required=["location"],
    )
    schema = to_json_schema(parameters)
    assert schema["type"] == "object"
    assert schema["required"] == ["location"]
    properties = schema["properties"]
    assert properties["location"] == {"type": "string", "description": "城市或地名"}
    assert properties["days"] == {"type": "integer", "minimum": 1, "maximum": 3}
    assert properties["flag"] == {"type": "boolean", "default": False}


def test_enum_and_string_constraints_are_kept() -> None:
    """enum / pattern / minLength 等约束要带过去。"""
    prop = ParameterProperty(
        type="string",
        enum=["a", "b"],
        pattern="^[ab]$",
        minLength=1,
        maxLength=1,
        examples=["a"],
    )
    assert property_to_schema(prop) == {
        "type": "string",
        "enum": ["a", "b"],
        "pattern": "^[ab]$",
        "minLength": 1,
        "maxLength": 1,
        "examples": ["a"],
    }


def test_nullable_becomes_type_union() -> None:
    """``nullable`` 用 ``type: [T, "null"]`` 表达，兼容面更广。"""
    prop = ParameterProperty(type="string", nullable=True)
    assert property_to_schema(prop) == {"type": ["string", "null"]}


def test_any_of_with_nullable_appends_null_branch() -> None:
    """``anyOf`` + nullable 时补一个 null 分支。"""
    prop = ParameterProperty(
        anyOf=[ParameterProperty(type="string"), ParameterProperty(type="integer")],
        nullable=True,
    )
    schema = property_to_schema(prop)
    assert schema["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
        {"type": "null"},
    ]


def test_nested_object_and_array_items() -> None:
    """嵌套对象、必填子字段与数组元素递归映射。"""
    prop = ParameterProperty(
        type="object",
        properties={"pattern": ParameterProperty(type="string")},
        required=["pattern"],
    )
    assert property_to_schema(prop) == {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }
    array = ParameterProperty(type="array", items=ParameterProperty(type="number"))
    assert property_to_schema(array) == {"type": "array", "items": {"type": "number"}}


def test_additional_properties_forms() -> None:
    """``additionalProperties`` 既可能是布尔也可能是子 schema。"""
    free = ParameterProperty(type="object", additionalProperties=True)
    assert property_to_schema(free) == {"type": "object", "additionalProperties": True}
    typed = ParameterProperty(
        type="object", additionalProperties=ParameterProperty(type="string")
    )
    assert property_to_schema(typed) == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }


def test_round_trip_through_bridge_conversion() -> None:
    """``blue → JSON Schema → blue`` 往返后关键信息不丢。"""
    original = ToolParameters(
        type="object",
        properties={
            "text": ParameterProperty(type="string", description="文本"),
            "count": ParameterProperty(type="integer", minimum=1),
            "kind": ParameterProperty(type="string", enum=["x", "y"]),
            "tags": ParameterProperty(
                type="array", items=ParameterProperty(type="string")
            ),
        },
        required=["text", "kind"],
    )
    schema = to_json_schema(original)
    restored = convert_input_schema(schema).parameters
    assert restored.required == ["text", "kind"]
    properties: dict[str, Any] = dict(restored.properties or {})
    assert properties["text"].type == "string"
    assert properties["text"].description == "文本"
    assert properties["count"].minimum == 1
    assert properties["kind"].enum == ["x", "y"]
    assert properties["tags"].type == "array"
