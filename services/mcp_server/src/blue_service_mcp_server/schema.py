"""把 ``infinity_agent`` 的工具参数 schema 转回 JSON Schema。

``services/mcp_bridge/schema.py`` 是「MCP → blue」方向；本模块是反方向：
``ToolParameters`` / ``ParameterProperty`` 是 OpenAI 风格的受限子集，MCP 客户端
要的是标准 JSON Schema。字段是前者的超集，所以映射是纯展开，不需要降级。

``nullable`` 用 ``type: [T, "null"]`` 表达（对不确定支持 ``anyOf`` 的客户端更稳）。
"""

from __future__ import annotations

from typing import Any

from infinity_agent import ParameterProperty, ToolParameters

__all__ = ["property_to_schema", "to_json_schema"]


def to_json_schema(parameters: ToolParameters) -> dict[str, Any]:
    """把工具参数 schema 转成 MCP 客户端可用的 JSON Schema。

    :param parameters: blue 侧工具的参数 schema。
    :return: ``{"type": "object", "properties": {...}, "required": [...]}``；
        无参数时 ``properties`` 为空对象、``required`` 省略。
    """
    properties: dict[str, Any] = {
        name: property_to_schema(prop)
        for name, prop in (parameters.properties or {}).items()
    }
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    required = list(parameters.required or [])
    if required:
        schema["required"] = required
    return schema


def property_to_schema(prop: ParameterProperty) -> dict[str, Any]:
    """把单个参数属性转成 JSON Schema 节点。

    :param prop: blue 侧参数属性。
    :return: 等价的 JSON Schema 节点（省略空字段）。
    """
    schema: dict[str, Any] = {}

    if prop.anyOf:
        variants = [property_to_schema(item) for item in prop.anyOf]
        if prop.nullable:
            variants.append({"type": "null"})
        schema["anyOf"] = variants
        return _with_common(schema, prop)

    if prop.type is not None:
        if prop.nullable:
            schema["type"] = [prop.type, "null"]
        else:
            schema["type"] = prop.type

    if prop.properties:
        schema["properties"] = {
            name: property_to_schema(child) for name, child in prop.properties.items()
        }
    if prop.required:
        schema["required"] = list(prop.required)
    if prop.items is not None:
        schema["items"] = property_to_schema(prop.items)
    if prop.additionalProperties is not None:
        additional = prop.additionalProperties
        schema["additionalProperties"] = (
            property_to_schema(additional)
            if isinstance(additional, ParameterProperty)
            else additional
        )
    return _with_common(schema, prop)


def _with_common(schema: dict[str, Any], prop: ParameterProperty) -> dict[str, Any]:
    """补上描述、枚举、默认值与数值/字符串约束。"""
    if prop.description:
        schema["description"] = prop.description
    if prop.enum is not None:
        schema["enum"] = list(prop.enum)
    if prop.default is not None:
        schema["default"] = prop.default
    for field_name in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "minLength",
        "maxLength",
        "examples",
    ):
        value = getattr(prop, field_name)
        if value is not None:
            schema[field_name] = value
    return schema
