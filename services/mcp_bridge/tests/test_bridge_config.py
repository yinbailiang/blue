"""MCP 桥配置测试：命名规则与配置一致性校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from blue_service_mcp_bridge.config import (
    TOOL_SEPARATOR,
    McpBridgeConfig,
    McpServerConfig,
    parse_tool_name,
    tool_name,
    worker_id,
)
from infinity_data import SandboxConfig, load
from pydantic import ValidationError


def test_defaults_allow_an_idle_bridge() -> None:
    """零配置：不连任何 server、不注册任何工具。"""
    config = McpBridgeConfig()
    assert config.servers == []
    assert config.tools == []
    assert config.call_timeout == 60.0
    assert config.enabled_servers == ()


def test_tool_name_and_worker_id_are_deterministic() -> None:
    """命名规则固定可预测：重启后租约标识不变。"""
    assert tool_name("fs", "read_file") == "fs__read_file"
    assert worker_id("fs", "read_file") == "mcp.fs.read_file"
    assert worker_id("fs", "read_file") == worker_id("fs", "read_file")
    assert TOOL_SEPARATOR == "__"


def test_parse_tool_name_round_trip() -> None:
    """``<server>__<tool>`` 能拆回，工具名本身可再含分隔符。"""
    assert parse_tool_name("fs__read_file") == ("fs", "read_file")
    assert parse_tool_name("fs__a__b") == ("fs", "a__b")


@pytest.mark.parametrize(
    "name",
    ["fs", "", "read_file", "__read", "fs__", "FS__x", "_fs__x", "1fs__x"],
)
def test_parse_tool_name_rejects_malformed_entries(name: str) -> None:
    """非法写法一律返回 ``None``（由配置层转成明确报错）。"""
    assert parse_tool_name(name) is None


def test_server_name_must_be_infd_identifier() -> None:
    """server 名必须匹配 ``[a-z][a-z0-9_]*``。"""
    with pytest.raises(ValidationError):
        McpServerConfig(name="FS", command="npx")
    with pytest.raises(ValidationError):
        McpServerConfig(name="1fs", command="npx")
    with pytest.raises(ValidationError):
        McpServerConfig(name="a__b", command="npx")


def test_server_command_must_not_be_blank() -> None:
    """空命令无法启动子进程，配置期就拒绝。"""
    with pytest.raises(ValidationError):
        McpServerConfig(name="fs", command="   ")


def test_unknown_keys_are_rejected() -> None:
    """配置里写错字段名要立刻失败，而不是被静默忽略。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate({"servers": [], "tool": ["fs__read_file"]})


def test_duplicate_server_names_rejected() -> None:
    """同名 server 会让工具名前缀撞车。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate(
            {
                "servers": [
                    {"name": "fs", "command": "npx"},
                    {"name": "fs", "command": "uvx"},
                ]
            }
        )


def test_duplicate_tool_entries_rejected() -> None:
    """白名单重复声明视为配置错误。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate(
            {
                "servers": [{"name": "fs", "command": "npx"}],
                "tools": ["fs__read_file", "fs__read_file"],
            }
        )


def test_tool_for_unknown_server_rejected() -> None:
    """白名单指向未声明的 server 直接失败。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate(
            {"servers": [{"name": "fs", "command": "npx"}], "tools": ["git__status"]}
        )


def test_tool_for_disabled_server_rejected() -> None:
    """禁用 server 却声明它的工具，属于自相矛盾。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate(
            {
                "servers": [{"name": "fs", "command": "npx", "enabled": False}],
                "tools": ["fs__read_file"],
            }
        )


def test_tools_without_servers_rejected() -> None:
    """没有 server 就没有工具来源。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate({"tools": ["fs__read_file"]})


def test_backoff_bounds_must_be_ordered() -> None:
    """退避上限不能小于基数。"""
    with pytest.raises(ValidationError):
        McpBridgeConfig.model_validate(
            {"reconnect_backoff": 5.0, "reconnect_backoff_max": 1.0}
        )


def test_enabled_servers_filters_disabled() -> None:
    """``enabled = false`` 的 server 不参与连接。"""
    config = McpBridgeConfig.model_validate(
        {
            "servers": [
                {"name": "fs", "command": "npx"},
                {"name": "git", "command": "uvx", "enabled": False},
            ]
        }
    )
    assert [server.name for server in config.enabled_servers] == ["fs"]
    assert config.server("git") is not None
    assert config.server("nope") is None


def test_config_model_is_the_runtime_model() -> None:
    """``CONFIG_MODEL`` 指向运行时模型（supervisor 投影与服务的契约）。"""
    import blue_service_mcp_bridge as service

    assert service.CONFIG_MODEL is McpBridgeConfig
    assert service.SERVICE_NAME == "mcp_bridge"


def test_manifest_template_defaults_match_runtime_model(tmp_path: Path) -> None:
    """``service.infd`` 的模板默认值必须能被运行时模型接受，避免两层漂移。"""
    manifest = Path(__file__).resolve().parents[1] / "service.infd"
    probe = tmp_path / "probe.infd"
    probe.write_text(
        f'!from p"{manifest}" import ServiceTemplate as Bridge\ninstance = Bridge()\n',
        encoding="utf-8",
    )
    result = load(
        probe,
        sandbox=SandboxConfig(
            allow_templates=[str(manifest.parent / "**")],
            allow_files=[str(manifest.parent / "**")],
        ),
    )
    assert not result.has_errors, result.diagnostics
    config = McpBridgeConfig.model_validate(result.value["instance"])
    assert config.servers == []
    assert config.tools == []
    assert config.call_timeout == 60.0
    assert config.call_timeout == pytest.approx(60.0)
