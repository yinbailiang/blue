"""协议层测试：主题命名、事件声明与注册。"""

from __future__ import annotations

import pytest
from infinity_bus import EventRegistry

from blue.protocol import (
    CatalogChangedPayload,
    CatalogQueryRequest,
    SessionPromptRequest,
    ToolCallRequest,
    Topic,
    TopicBook,
)


def test_topic_book_prefixes_namespace() -> None:
    """事件名统一带命名空间前缀。"""
    topics = TopicBook("demo")

    assert topics.name(Topic.TOOL_CALL) == "demo.tool.call"
    assert topics.name(Topic.SESSION_PROMPT) == "demo.session.prompt"
    assert topics.namespace == "demo"


def test_topic_book_registers_declarations() -> None:
    """所有主题都能注册到事件注册表，且负载类型正确。"""
    topics = TopicBook("blue")
    registry = EventRegistry()
    topics.register(registry)

    assert sorted(registry.list_names()) == sorted(topics.topics().values())
    declaration = registry.get(topics.name(Topic.TOOL_CALL))
    assert declaration is not None
    assert declaration.payload_type is ToolCallRequest


def test_topic_book_rejects_blank_namespace() -> None:
    """空命名空间不允许。"""
    with pytest.raises(ValueError, match="命名空间"):
        TopicBook("  ")


def test_request_payloads_carry_rpc_fields() -> None:
    """请求负载必须可被 RPC 模板识别（含 session_id / request_id）。"""
    request = CatalogQueryRequest(session_id="s1", request_id="r1")
    prompt = SessionPromptRequest(session_id="s1", request_id="r2", text="你好")

    assert request.session_id == "s1"
    assert prompt.text == "你好"


def test_catalog_payload_defaults_to_empty_tools() -> None:
    """目录广播的默认工具列表互不共享（default_factory）。"""
    first = CatalogChangedPayload(version=1)
    second = CatalogChangedPayload(version=2)

    assert first.tools == []
    assert first.tools is not second.tools
