"""服务侧运行时辅助测试：运行时配置定位与校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

import blue.runtime as runtime

from blue.errors import RuntimeConfigError
from blue.runtime import (
    DEFAULT_RUNTIME_CONFIG,
    load_runtime_config,
    runtime_config_path,
)


class DemoConfig(BaseModel):
    value: str = "default"


def test_load_runtime_config_from_explicit_path(tmp_path: Path) -> None:
    """显式路径优先于环境变量与 cwd。"""
    path = tmp_path / "config.json"
    path.write_text('{"value": "explicit"}', encoding="utf-8")

    config = load_runtime_config(DemoConfig, path)

    assert config.value == "explicit"


def test_load_runtime_config_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLUE_CONFIG_FILE 指向的配置被读取。"""
    path = tmp_path / "runtime.json"
    path.write_text('{"value": "env"}', encoding="utf-8")
    monkeypatch.setenv("BLUE_CONFIG_FILE", str(path))

    assert load_runtime_config(DemoConfig).value == "env"


def test_runtime_config_path_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置环境变量时使用 cwd 下的默认文件名。"""
    monkeypatch.delenv("BLUE_CONFIG_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert runtime_config_path() == (tmp_path / DEFAULT_RUNTIME_CONFIG).resolve()


def test_load_runtime_config_missing_file(tmp_path: Path) -> None:
    """文件不存在时给出明确错误。"""
    with pytest.raises(RuntimeConfigError, match="不存在"):
        load_runtime_config(DemoConfig, tmp_path / "missing.json")


def test_load_runtime_config_invalid_json(tmp_path: Path) -> None:
    """内容非法时给出校验错误。"""
    path = tmp_path / "bad.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="校验失败"):
        load_runtime_config(DemoConfig, path)


def test_registry_ttl_reads_injected_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """supervisor 注入 ``BLUE_REGISTRY_TTL`` 时以其为准。"""
    monkeypatch.setenv("BLUE_REGISTRY_TTL", "7.5")

    assert runtime.registry_ttl() == 7.5


def test_registry_ttl_defaults_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失时回退缺省租约（15 秒）。"""
    monkeypatch.delenv("BLUE_REGISTRY_TTL", raising=False)

    assert runtime.registry_ttl() == 15.0
    assert runtime.registry_ttl(default=3.0) == 3.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-2"])
def test_registry_ttl_rejects_bad_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """坏值不能变成「立刻过期」的租约。"""
    monkeypatch.setenv("BLUE_REGISTRY_TTL", raw)

    assert runtime.registry_ttl() == 15.0
