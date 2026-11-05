"""服务侧运行时辅助测试：运行时配置定位与校验。"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from blue_contracts_tool import EVENTS as TOOL_EVENTS
from infinity_bus import EventDeclaration
from pydantic import BaseModel, ValidationError

import blue.runtime as runtime
from blue.errors import RuntimeConfigError
from blue.runtime import (
    DEFAULT_RUNTIME_CONFIG,
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


@pytest.fixture(autouse=True)
def _isolate_infra() -> Iterator[None]:
    """基础设施配置有进程级缓存，每个用例前后都清空。"""
    runtime.reset_infra_config()
    yield
    runtime.reset_infra_config()


def _install_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> Path:
    """写一份运行时配置并指向它，模拟 supervisor 下发的基础设施参数。"""
    config_path = tmp_path / DEFAULT_RUNTIME_CONFIG
    infra: dict[str, object] = {
        "project_root": str(tmp_path),
        "bus_url": runtime.DEFAULT_BUS_URL,
        "state_dir": str(tmp_path / ".blue"),
    }
    infra.update(overrides)
    config_path.write_text(
        json.dumps({runtime.CONFIG_SECTION: {}, runtime.INFRA_SECTION: infra}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUE_CONFIG_FILE", str(config_path))
    return config_path


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
    # 在 supervisor 拉起的服务进程里跑测试时，这个变量还在，会被当成「由
    # supervisor 启动」而拒绝回退；清掉才能模拟单进程手工运行
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    assert runtime_config_path() == (tmp_path / DEFAULT_RUNTIME_CONFIG).resolve()


def test_load_runtime_config_missing_file(tmp_path: Path) -> None:
    """文件不存在时给出明确错误。"""
    with pytest.raises(RuntimeConfigError, match="不存在"):
        load_runtime_config(DemoConfig, tmp_path / "missing.json")


def test_load_runtime_config_invalid_json(tmp_path: Path) -> None:
    """内容非法时明确报告「不是合法 JSON」，而不是笼统的校验失败。"""
    path = tmp_path / "bad.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="不是合法 JSON"):
        load_runtime_config(DemoConfig, path)


@pytest.fixture(autouse=True)
def _reset_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """``configure_workspace()`` 写入的进程级状态不应泄漏到其它用例。"""
    monkeypatch.setattr(runtime, "_workspace", None)


def test_project_root_reads_infra_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor 下发的项目根优先。"""
    _install_infra(tmp_path, monkeypatch, project_root=str(tmp_path / "root"))

    assert project_root() == (tmp_path / "root").resolve()


def test_project_root_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有基础设施配置时回退进程当前目录。"""
    monkeypatch.delenv("BLUE_CONFIG_FILE", raising=False)
    # 在 supervisor 拉起的服务进程里跑测试时，这个变量还在，会被当成「由
    # supervisor 启动」而拒绝回退；清掉才能模拟单进程手工运行
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    assert project_root() == tmp_path.resolve()


def test_resolve_workspace_defaults_to_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """workdir 缺省为项目根。"""
    _install_infra(tmp_path, monkeypatch)

    assert resolve_workspace(WorkspaceConfig()) == tmp_path.resolve()


def test_resolve_workspace_relative_to_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对 workdir 按项目根解析。"""
    _install_infra(tmp_path, monkeypatch)

    config = WorkspaceConfig(workdir="packages/app")

    assert resolve_workspace(config) == (tmp_path / "packages/app").resolve()


def test_resolve_workspace_accepts_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绝对 workdir 不受项目根影响。"""
    _install_infra(tmp_path, monkeypatch, project_root=str(tmp_path / "root"))
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
    _install_infra(tmp_path, monkeypatch)

    assert workspace().workdir == tmp_path.resolve()


def test_configure_workspace_sets_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入配置后锚点切换到解析结果。"""
    _install_infra(tmp_path, monkeypatch)

    configure_workspace(WorkspaceConfig(workdir="sub"))

    assert workspace().workdir == (tmp_path / "sub").resolve()
    assert workspace().resolve("a.txt") == (tmp_path / "sub" / "a.txt").resolve()


def test_workspace_rejects_empty_path() -> None:
    """空路径不再被静默解析为锚点目录本身。"""
    with pytest.raises(ValueError, match="不能为空"):
        Workspace(Path("/tmp")).resolve("   ")


def test_registry_ttl_reads_infra_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor 下发的租约时长优先。"""
    _install_infra(tmp_path, monkeypatch, registry_ttl=7.5)

    assert runtime.registry_ttl() == 7.5


def test_registry_ttl_defaults_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有基础设施配置时回退缺省租约（15 秒）。"""
    monkeypatch.delenv("BLUE_CONFIG_FILE", raising=False)
    # 在 supervisor 拉起的服务进程里跑测试时，这个变量还在，会被当成「由
    # supervisor 启动」而拒绝回退；清掉才能模拟单进程手工运行
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    assert runtime.registry_ttl() == 15.0


@pytest.mark.parametrize("raw", [0, -2])
def test_infra_config_rejects_non_positive_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: float
) -> None:
    """非正数租约在加载时就报错，不会变成「立刻过期」的租约。"""
    _install_infra(tmp_path, monkeypatch, registry_ttl=raw)

    with pytest.raises(RuntimeConfigError):
        runtime.registry_ttl()


def test_infra_config_rejects_unknown_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拼错的字段直接报错，避免静默忽略。"""
    _install_infra(tmp_path, monkeypatch, registry_ttll=7.5)

    with pytest.raises(RuntimeConfigError):
        runtime.registry_ttl()


def test_infra_config_requires_section_when_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor 启动的进程缺少 infra 段时直接报错，不回退默认总线。"""
    path = tmp_path / DEFAULT_RUNTIME_CONFIG
    path.write_text(json.dumps({runtime.CONFIG_SECTION: {}}), encoding="utf-8")
    monkeypatch.setenv("BLUE_CONFIG_FILE", str(path))
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    with pytest.raises(RuntimeConfigError, match="缺少 'infra' 段"):
        runtime.load_infra_config()


def test_infra_config_requires_file_when_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor 下发的配置文件缺失时直接报错，而不是回退默认值。"""
    monkeypatch.setenv("BLUE_CONFIG_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    with pytest.raises(RuntimeConfigError, match="supervisor 下发的运行时配置不可用"):
        runtime.load_infra_config()


def test_infra_config_falls_back_for_manual_run_without_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """手工运行时缺少 infra 段仍回退开发默认值。"""
    monkeypatch.delenv("BLUE_CONFIG_FILE", raising=False)
    # 在 supervisor 拉起的服务进程里跑测试时，这个变量还在，会被当成「由
    # supervisor 启动」而拒绝回退；清掉才能模拟单进程手工运行
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / DEFAULT_RUNTIME_CONFIG).write_text(
        json.dumps({runtime.CONFIG_SECTION: {}}), encoding="utf-8"
    )

    config = runtime.load_infra_config()

    assert config.bus_url == runtime.DEFAULT_BUS_URL
    assert config.state_dir == Path(".blue")


class _FirstDuplicate(EventDeclaration):
    """与下一个类同名的事件声明。"""

    name = "demo.duplicate"


class _SecondDuplicate(EventDeclaration):
    """与上一个类同名的事件声明。"""

    name = "demo.duplicate"


def test_build_runtime_scans_contract_events_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件注册由 ``infra.contracts`` 驱动，不靠调用方传列表。"""
    _install_infra(tmp_path, monkeypatch, contracts={"tool": "blue_contracts_tool"})
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    bus = runtime.build_runtime()

    assert bus.member_id == "demo.demo"
    names = set(bus._events.list_names())  # pyright: ignore[reportPrivateUsage]
    assert names == {event.name for event in TOOL_EVENTS}


def test_build_runtime_explicit_events_override_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式传入 ``events`` 时不再扫描配置（单测/特殊进程用）。"""
    _install_infra(tmp_path, monkeypatch, contracts={"tool": "no_such_module"})
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    bus = runtime.build_runtime("demo", events=())

    assert bus._events.list_names() == []  # pyright: ignore[reportPrivateUsage]


def test_load_contract_events_reports_missing_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约模块没装/名字写错时给出可操作的错误。"""
    _install_infra(tmp_path, monkeypatch, contracts={"tool": "no_such_module"})
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    with pytest.raises(RuntimeConfigError, match="不可用"):
        runtime.load_contract_events()


def test_load_contract_events_rejects_modules_without_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入得到但没导 ``EVENTS`` 的模块不能静默跳过。"""
    _install_infra(tmp_path, monkeypatch, contracts={"tool": "json"})
    monkeypatch.setenv("BLUE_SERVICE_NAME", "demo")

    with pytest.raises(RuntimeConfigError, match="没有导出 EVENTS"):
        runtime.load_contract_events()


def test_load_contract_events_rejects_duplicate_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个契约声明同名事件时直接报错，避免注册表静默覆盖。"""
    first = types.ModuleType("demo_contract_first")
    second = types.ModuleType("demo_contract_second")
    first.__dict__["EVENTS"] = (_FirstDuplicate,)
    second.__dict__["EVENTS"] = (_SecondDuplicate,)
    monkeypatch.setitem(sys.modules, "demo_contract_first", first)
    monkeypatch.setitem(sys.modules, "demo_contract_second", second)

    with pytest.raises(RuntimeConfigError, match="重名"):
        runtime.load_contract_events(
            {"a": "demo_contract_first", "b": "demo_contract_second"}
        )
