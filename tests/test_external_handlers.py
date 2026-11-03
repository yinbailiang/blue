"""外置程序测试：工具模块可注册、外置程序拒绝进程内传输。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from handlers.support import load_settings_for_external
from handlers.tools import TOOLS
from infinity_agent.tools import ToolRegistry

from blue.config import ConfigError


def test_tool_programs_are_registered() -> None:
    """内置外置工具清单包含全部随仓库提供的程序。"""
    assert set(TOOLS) == {
        "clock",
        "echo",
        "edit_file",
        "read_file",
        "run_shell",
        "text_stats",
        "write_file",
    }


@pytest.mark.parametrize("alias", sorted(TOOLS))
def test_tool_module_exposes_registrable_callable(alias: str) -> None:
    """每个工具模块暴露可被签名推断注册的 TOOL。"""
    module = importlib.import_module(TOOLS[alias])
    registry = ToolRegistry()
    registry.register(module.TOOL)

    definition = registry.definitions[0]
    assert definition.function.name
    assert definition.function.parameters.properties


def test_external_settings_reject_memory_transport(tmp_path: Path) -> None:
    """外置程序不能使用仅限进程内的 memory 传输。"""
    path = tmp_path / "blue.infd"
    path.write_text(
        'bus { transport: in("rabbit", "memory") = "memory" }\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="rabbit"):
        load_settings_for_external(str(path))
