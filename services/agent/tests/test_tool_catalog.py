"""工具目录测试：schema 体量的统计口径必须与「真正发给模型的请求体」一致。"""

from __future__ import annotations

from blue_contracts_tool import ToolDefinition
from blue_service_agent.catalog import schema_chars
from infinity_agent.models.tools import ParameterProperty


def _tool(name: str = "demo") -> ToolDefinition:
    """造一个带参数的演示工具。

    ``ParameterProperty`` 有十几个可选字段，不排除 ``None`` 时它们会全部被写成
    ``"anyOf": null, "enum": null, …``——正是这些 null 让全量 dump 比真实请求体
    大 2.4 倍。
    """
    tool = ToolDefinition.create(name, "演示工具")
    tool.function.parameters.properties["query"] = ParameterProperty(
        type="string", description="查询词"
    )
    tool.function.parameters.required.append("query")
    return tool


def test_schema_chars_matches_the_client_serialisation() -> None:
    """口径 = 客户端口径（``exclude_none``），不是全量 dump。"""
    tools = [_tool()]

    expected = sum(len(tool.model_dump_json(exclude_none=True)) for tool in tools)

    assert schema_chars(tools) == expected


def test_schema_chars_does_not_pay_for_none_fields() -> None:
    """可选字段的 ``null`` 不算进开销：虚拟屏幕那类工具差得最多。"""
    tools = [_tool("kwin__session_start")]

    raw = sum(len(tool.model_dump_json()) for tool in tools)

    assert schema_chars(tools) < raw
    assert raw - schema_chars(tools) > 100


def test_schema_chars_sums_the_catalog() -> None:
    """多个工具按份累加，空目录为 0。"""
    assert schema_chars([]) == 0
    assert schema_chars([_tool("a"), _tool("b")]) == 2 * schema_chars([_tool("a")])
