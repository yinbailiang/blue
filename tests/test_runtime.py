"""服务侧运行时辅助测试：运行时配置定位与校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import blue.runtime as runtime
from blue.errors import RuntimeConfigError
from blue.runtime import (
    DEFAULT_RUNTIME_CONFIG,
    PROJECT_ROOT_ENV,
    Workspace,
    WorkspaceConfig,
    configure_workspace,
    load_runtime_config,
    project_root,
    resolve_workspace,
    resolve_workspace_path,
    runtime_config_path,
    workspace,
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


@pytest.fixture(autouse=True)
def _reset_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """``configure_workspace()`` 写入的进程级状态不应泄漏到其它用例。"""
    monkeypatch.setattr(runtime, "_workspace", None)


def test_project_root_prefers_injected_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor 注入的项目根优先。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))

    assert project_root() == tmp_path.resolve()


def test_project_root_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """未注入时回退进程当前目录。"""
    monkeypatch.delenv(PROJECT_ROOT_ENV, raising=False)

    assert project_root() == Path.cwd().resolve()


def test_resolve_workspace_defaults_to_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """workdir 缺省为项目根。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))

    assert resolve_workspace(WorkspaceConfig()) == tmp_path.resolve()


def test_resolve_workspace_relative_to_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对 workdir 按项目根解析。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))

    config = WorkspaceConfig(workdir="packages/app")

    assert resolve_workspace(config) == (tmp_path / "packages/app").resolve()


def test_resolve_workspace_accepts_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绝对 workdir 不受项目根影响。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path / "root"))
    target = tmp_path / "elsewhere"

    config = WorkspaceConfig(workdir=str(target))

    assert resolve_workspace(config) == target.resolve()


def test_resolve_workspace_rejects_unknown_field() -> None:
    """拼错的配置字段直接报错，避免被静默忽略。"""
    with pytest.raises(ValidationError):
        WorkspaceConfig.model_validate({"workdirs": "/tmp"})


def test_resolve_workspace_path_keeps_absolute(tmp_path: Path) -> None:
    """绝对路径原样解析，不受锚点影响。"""
    anchor = tmp_path / "anchor"
    target = tmp_path / "other" / "a.txt"

    assert resolve_workspace_path(str(target), anchor) == target.resolve()


def test_resolve_workspace_path_joins_relative(tmp_path: Path) -> None:
    """相对路径拼到锚点下。"""
    anchor = tmp_path / "anchor"

    assert (
        resolve_workspace_path("a/b.txt", anchor) == (anchor / "a" / "b.txt").resolve()
    )


def test_workspace_defaults_to_project_root_without_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未调用 configure_workspace 时锚定项目根。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))

    assert workspace().workdir == tmp_path.resolve()


def test_configure_workspace_sets_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入配置后锚点切换到解析结果。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))

    configure_workspace(WorkspaceConfig(workdir="sub"))

    assert workspace().workdir == (tmp_path / "sub").resolve()
    assert workspace().resolve("a.txt") == (tmp_path / "sub" / "a.txt").resolve()


def test_workspace_rejects_empty_path() -> None:
    """空路径不再被静默解析为锚点目录本身。"""
    with pytest.raises(ValueError, match="不能为空"):
        Workspace(Path("/tmp")).resolve("   ")


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
