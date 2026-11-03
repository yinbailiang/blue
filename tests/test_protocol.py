"""事件契约测试：域归属、唯一性与负载契约。"""

from __future__ import annotations

import pytest
from blue_contracts_session import (
    EVENTS as SESSION_EVENTS,
)
from blue_contracts_session import (
    SessionPromptEvent,
    SessionPromptRequest,
    SessionStreamEvent,
    SessionStreamPayload,
)
from blue_contracts_tool import (
    EVENTS as TOOL_EVENTS,
)
from blue_contracts_tool import (
    CatalogChangedEvent,
    CatalogChangedPayload,
    CatalogQueryRequest,
    ToolCallEvent,
    ToolCallRequest,
)
from infinity_bus import EventRegistry

EVENTS = (*TOOL_EVENTS, *SESSION_EVENTS)
NAMES = tuple(event.name for event in EVENTS)


def test_event_names_are_fixed_constants() -> None:
    """事件名是稳定常量，不含命名空间前缀。"""
    assert ToolCallEvent.name == "tool.call"
    assert SessionPromptEvent.name == "session.prompt"
    assert SessionStreamEvent.name == "session.stream"
    assert CatalogChangedEvent.name == "catalog.changed"
    assert tuple(event.name for event in EVENTS) == NAMES
    assert len(set(NAMES)) == len(NAMES)


def test_contract_packages_register_their_events() -> None:
    """每个契约包只注册自己领域的事件，且负载类型可解析。"""
    tool_registry = EventRegistry()
    for event in TOOL_EVENTS:
        tool_registry.register(event)
    assert set(tool_registry.list_names()) == {event.name for event in TOOL_EVENTS}

    session_registry = EventRegistry()
    for event in SESSION_EVENTS:
        session_registry.register(event)
    assert set(session_registry.list_names()) == {
        event.name for event in SESSION_EVENTS
    }


def test_duplicate_registration_is_rejected() -> None:
    """重复注册同名事件会抛错，防止静默覆盖。"""
    registry = EventRegistry()
    for event in EVENTS:
        registry.register(event)

    with pytest.raises(ValueError, match="重复的事件声明"):
        registry.register(ToolCallEvent)


def test_tool_call_carries_target() -> None:
    """``tool.call`` 靠载荷里的工具名定位目标。"""
    request = ToolCallRequest(
        session_id="s1", request_id="r1", tool="echo", arguments={"text": "hi"}
    )

    assert request.tool == "echo"
    assert request.arguments == {"text": "hi"}


def test_request_payloads_carry_rpc_fields() -> None:
    """请求负载可被 RPC 模板识别（含 session_id / request_id）。"""
    query = CatalogQueryRequest(session_id="s1", request_id="r1")
    prompt = SessionPromptRequest(session_id="s1", request_id="r2", text="你好")

    assert query.session_id == "s1"
    assert prompt.text == "你好"


def test_catalog_payload_default_factory_is_per_instance() -> None:
    """目录广播的默认工具列表互不共享。"""
    first = CatalogChangedPayload(version=1)
    second = CatalogChangedPayload(version=2)

    assert first.tools == []
    assert first.tools is not second.tools


def test_every_event_declares_payload_type() -> None:
    """所有跨进程事件都必须声明负载类型，才能参与校验与编解码。"""
    assert all(event.payload_type is not None for event in EVENTS)


def test_event_ownership_prefix() -> None:
    """事件名前缀必须匹配所属域契约包。"""
    assert all(event.name.startswith(("tool.", "catalog.")) for event in TOOL_EVENTS)
    assert all(event.name.startswith("session.") for event in SESSION_EVENTS)


def test_session_stream_payload_is_broadcast_data() -> None:
    """流式事件是广播数据，不要求 RPC 字段，但保留 session_id/request_id。"""
    payload = SessionStreamPayload(
        session_id="s1",
        request_id="r1",
        kind="text",
        step=2,
        text="你好",
    )

    assert payload.kind == "text"
    assert payload.text == "你好"
    assert SessionStreamEvent.payload_type is SessionStreamPayload
