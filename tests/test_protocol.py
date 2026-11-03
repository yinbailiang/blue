"""协议层测试：事件名稳定、声明注册与负载契约。"""

from __future__ import annotations

import pytest
from infinity_bus import EventRegistry

from blue.protocol import (
    ALL_EVENTS,
    EVENT_NAMES,
    CatalogChangedEvent,
    CatalogChangedPayload,
    CatalogQueryRequest,
    SessionPromptEvent,
    SessionPromptRequest,
    ToolCallEvent,
    ToolCallRequest,
    register_events,
)


def test_event_names_are_fixed_constants() -> None:
    """事件名是稳定常量，不含命名空间前缀。"""
    assert ToolCallEvent.name == "tool.call"
    assert SessionPromptEvent.name == "session.prompt"
    assert CatalogChangedEvent.name == "catalog.changed"
    assert tuple(event.name for event in ALL_EVENTS) == EVENT_NAMES
    assert len(set(EVENT_NAMES)) == len(EVENT_NAMES)


def test_register_events_fills_registry() -> None:
    """一次注册即可获得全部事件声明与负载类型。"""
    registry = EventRegistry()
    register_events(registry)

    assert sorted(registry.list_names()) == sorted(EVENT_NAMES)
    declaration = registry.get(ToolCallEvent.name)
    assert declaration is not None
    assert declaration.payload_type is ToolCallRequest


def test_duplicate_registration_is_rejected() -> None:
    """重复注册同名事件会抛错，防止静默覆盖。"""
    registry = EventRegistry()
    register_events(registry)

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
    assert all(event.payload_type is not None for event in ALL_EVENTS)
