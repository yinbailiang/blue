"""服务端桥的设置校验测试：白名单、超时与危险工具门禁。"""

from __future__ import annotations

import pytest
from blue_service_mcp_server.config import (
    DEFAULT_PROTOCOL_VERSION,
    DEFAULT_TIMEOUT,
    UNSAFE_TOOLS,
    McpServerSettings,
)
from pydantic import ValidationError


def test_minimal_settings() -> None:
    """只要给白名单就能工作，其余走缺省。"""
    settings = McpServerSettings(expose=["clock"])
    assert settings.expose == ["clock"]
    assert settings.timeout == DEFAULT_TIMEOUT
    assert settings.server_name == "blue"
    assert settings.allow_unsafe is False
    assert DEFAULT_PROTOCOL_VERSION == "2025-06-18"


def test_expose_is_required_and_non_empty() -> None:
    """不支持「全部」：必须显式列出工具。"""
    with pytest.raises(ValidationError):
        McpServerSettings(expose=[])
    with pytest.raises(ValidationError):
        McpServerSettings.model_validate({})


def test_expose_rejects_duplicates_and_bad_names() -> None:
    """重复与非法工具名直接失败。"""
    with pytest.raises(ValidationError):
        McpServerSettings(expose=["clock", "clock"])
    with pytest.raises(ValidationError):
        McpServerSettings(expose=["Clock"])
    with pytest.raises(ValidationError):
        McpServerSettings(expose=["run-shell"])


@pytest.mark.parametrize("name", sorted(UNSAFE_TOOLS))
def test_unsafe_tools_require_explicit_opt_in(name: str) -> None:
    """危险工具默认拒绝，必须显式 ``allow_unsafe``。"""
    with pytest.raises(ValidationError, match="allow_unsafe"):
        McpServerSettings(expose=[name])
    settings = McpServerSettings(expose=[name], allow_unsafe=True)
    assert settings.expose == [name]


def test_safe_tools_are_allowed_by_default() -> None:
    """只读类工具无需额外开关。"""
    settings = McpServerSettings(expose=["clock", "text_stats", "weather", "read_file"])
    assert settings.allow_unsafe is False


def test_timeout_must_be_positive() -> None:
    """超时非正数没有意义。"""
    with pytest.raises(ValidationError):
        McpServerSettings(expose=["clock"], timeout=0)


def test_unknown_fields_rejected() -> None:
    """写错字段名要立刻失败。"""
    with pytest.raises(ValidationError):
        McpServerSettings.model_validate({"expose": ["clock"], "exposing": ["x"]})
