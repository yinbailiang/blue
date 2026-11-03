"""echo 工具测试。"""

from __future__ import annotations

from blue_service_echo import echo


def test_echo_returns_text_and_length() -> None:
    """回显原文并给出字符数。"""
    result = echo("你好")
    assert result.text == "你好"
    assert result.length == 2
