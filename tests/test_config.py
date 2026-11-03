"""配置层测试：``.infd`` 编译、字段校验与凭证解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from blue.config import AgentSettings, ConfigError, load_settings

VALID = """
agent {
  provider: in("deepseek", "openai") = "openai"
  model: str = "gpt-4o-mini"
  system_prompt: str = "测试提示词"
  max_steps: <int, range(1, 32)> = 3
  tool_timeout: <float, positive> = 2.5
}

bus {
  transport: in("rabbit", "memory") = "memory"
}

registry {
  ttl: <float, positive> = 5.0
  sweep_interval: <float, positive> = 0.5
}
"""


def _write(tmp_path: Path, source: str) -> Path:
    """把配置源码写入临时 ``.infd`` 文件。"""
    path = tmp_path / "blue.infd"
    path.write_text(source, encoding="utf-8")
    return path


def test_load_settings_parses_all_sections(tmp_path: Path) -> None:
    """配置各段都能正确降维为强类型模型。"""
    settings = load_settings(_write(tmp_path, VALID))

    assert settings.agent.provider == "openai"
    assert settings.agent.model == "gpt-4o-mini"
    assert settings.agent.system_prompt == "测试提示词"
    assert settings.agent.max_steps == 3
    assert settings.agent.tool_timeout == pytest.approx(2.5)
    assert settings.bus.transport == "memory"
    assert settings.registry.ttl == pytest.approx(5.0)
    assert settings.registry.sweep_interval == pytest.approx(0.5)


def test_load_settings_applies_defaults(tmp_path: Path) -> None:
    """省略的段回落默认值。"""
    settings = load_settings(_write(tmp_path, 'agent { model: str = "x" }\n'))

    assert settings.agent.provider == "deepseek"
    assert settings.bus.transport == "rabbit"
    assert settings.registry.ttl > 0


def test_load_settings_authorizes_declared_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式 ``!env import`` 的变量被授权读取并注入配置。"""
    monkeypatch.setenv("BLUE_TEST_KEY", "sk-from-env")
    source = """
!env import BLUE_TEST_KEY as test_key

agent {
  model: str = "deepseek-chat"
  api_key: str = $test_key
}
"""
    settings = load_settings(_write(tmp_path, source))

    assert settings.agent.api_key == "sk-from-env"
    assert settings.agent.resolve_api_key() == "sk-from-env"


def test_load_settings_reports_missing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明的环境变量不存在时给出明确错误。"""
    monkeypatch.delenv("BLUE_MISSING_KEY", raising=False)
    source = """
!env import BLUE_MISSING_KEY as missing

agent {
  api_key: str = $missing
}
"""
    with pytest.raises(ConfigError, match="BLUE_MISSING_KEY"):
        load_settings(_write(tmp_path, source))


def test_load_settings_rejects_constraint_violation(tmp_path: Path) -> None:
    """``.infd`` 约束不满足时抛 ConfigError。"""
    source = """
agent {
  max_steps: <int, range(1, 32)> = 99
}
"""
    with pytest.raises(ConfigError, match="配置编译失败"):
        load_settings(_write(tmp_path, source))


def test_load_settings_rejects_unknown_enum(tmp_path: Path) -> None:
    """枚举约束之外的取值被拒绝。"""
    source = """
bus {
  transport: in("rabbit", "memory") = "kafka"
}
"""
    with pytest.raises(ConfigError, match="配置编译失败"):
        load_settings(_write(tmp_path, source))


def test_load_settings_rejects_missing_file(tmp_path: Path) -> None:
    """配置文件不存在时给出明确错误。"""
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_settings(tmp_path / "missing.infd")


def test_resolve_api_key_prefers_literal() -> None:
    """配置内直接注入的密钥优先于环境变量。"""
    settings = AgentSettings(api_key="sk-literal", api_key_env="BLUE_TEST_KEY")

    assert settings.resolve_api_key() == "sk-literal"


def test_resolve_api_key_falls_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未直接注入时回落到 ``api_key_env`` 指向的环境变量。"""
    monkeypatch.setenv("BLUE_TEST_KEY", "sk-env")
    settings = AgentSettings(api_key="", api_key_env="BLUE_TEST_KEY")
    assert settings.resolve_api_key() == "sk-env"

    monkeypatch.delenv("BLUE_TEST_KEY")
    with pytest.raises(ConfigError, match="BLUE_TEST_KEY"):
        settings.resolve_api_key()
