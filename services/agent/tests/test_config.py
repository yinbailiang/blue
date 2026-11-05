"""Agent 配置测试：压缩相关默认值必须与服务模板一致，开关可关、非法值被拒。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from blue_service_agent.config import AgentConfig
from pydantic import ValidationError

TEMPLATE = Path(__file__).resolve().parents[1] / "service.infd"


def _template_default(field: str) -> str:
    """从 ``service.infd`` 里取出某个字段声明的默认值字面量。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{field}:.*?=\s*(\S+)\s*$", text, re.MULTILINE)
    assert match is not None, f"模板里没有 {field}"
    return match.group(1)


def test_compaction_defaults_match_the_service_template() -> None:
    """代码默认值必须与 ``service.infd`` 模板一致。

    曾经 ``AgentConfig`` 默认 60、模板 300、README 也写 300——绕过模板构造配置的
    入口（脚本、测试）拿到的口径与线上不同。这里把两者绑死。
    """
    config = AgentConfig()

    assert _template_default("compact_keep_messages") == str(
        config.compact_keep_messages
    )
    assert _template_default("compact_boundary_step") == str(
        config.compact_boundary_step
    )
    assert _template_default("compact_attachment_chars") == str(
        config.compact_attachment_chars
    )
    assert _template_default("compact_target_chars") == str(config.compact_target_chars)


def test_compaction_switches_can_be_disabled() -> None:
    """分界量化与附件当量都能关掉（``0``），回到逐条移动 / 只算文本。"""
    config = AgentConfig(compact_boundary_step=0, compact_attachment_chars=0)

    assert config.compact_boundary_step == 0
    assert config.compact_attachment_chars == 0
    assert config.compact_history is True


def test_negative_boundary_step_is_rejected() -> None:
    """步长不能为负。"""
    with pytest.raises(ValidationError):
        AgentConfig(compact_boundary_step=-1)
