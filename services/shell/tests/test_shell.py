"""shell 工具测试。"""

from __future__ import annotations

import asyncio
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import blue_service_shell as shell
import pytest
from blue_service_shell import MAX_OUTPUT_CHARS, run_shell


@pytest.fixture(autouse=True)
def _isolate_default_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``configure()`` 写入的进程级状态不应泄漏到其它用例。"""
    monkeypatch.setattr(shell, "_default_workdir", None)


def test_run_shell_returns_stdout_and_exit_code() -> None:
    """成功命令返回标准输出与退出码 0。"""
    result = asyncio.run(run_shell("printf 'hello'"))
    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""
    assert result.timed_out is False


def test_run_shell_uses_cwd(tmp_path: Path) -> None:
    """cwd 参数成为命令的工作目录。"""
    result = asyncio.run(run_shell("pwd", cwd=str(tmp_path)))
    assert result.cwd == str(tmp_path.resolve())
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_run_shell_reports_nonzero_exit() -> None:
    """非零退出码与标准错误原样返回。"""
    result = asyncio.run(run_shell("echo boom >&2; exit 3"))
    assert result.success is False
    assert result.exit_code == 3
    assert result.stderr.strip() == "boom"


def test_run_shell_times_out() -> None:
    """超时后强制结束并标记 timed_out。"""
    result = asyncio.run(run_shell("sleep 10", timeout=0.2))
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.success is False
    assert result.duration_ms < 5000


def test_run_shell_truncates_long_output() -> None:
    """超长输出保留首尾并插入省略标记。"""
    code = "import sys; sys.stdout.write('a' * 50000)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    result = asyncio.run(run_shell(command))
    assert result.success is True
    assert "已省略" in result.stdout
    assert len(result.stdout) < 50_000
    assert result.stdout.startswith("a")
    assert result.stdout.endswith("a")


def test_run_shell_rejects_empty_command() -> None:
    """空命令直接报错。"""
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(run_shell("   "))


def test_run_shell_rejects_missing_cwd(tmp_path: Path) -> None:
    """工作目录不存在时报错。"""
    with pytest.raises(ValueError, match="工作目录不存在"):
        asyncio.run(run_shell("pwd", cwd=str(tmp_path / "missing")))


def test_max_output_chars_is_positive() -> None:
    """输出上限是有效正数。"""
    assert MAX_OUTPUT_CHARS > 0


def test_project_root_prefers_injected_env(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """supervisor 下发的项目根即项目根。"""
    install_infra(tmp_path)
    assert shell.project_root() == tmp_path.resolve()


def test_project_root_falls_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有基础设施配置时退回进程当前目录。"""
    monkeypatch.delenv("BLUE_CONFIG_FILE", raising=False)
    # 在 supervisor 拉起的服务进程里跑测试时，这个变量还在，会被当成「由
    # supervisor 启动」而拒绝回退；清掉才能模拟单进程手工运行
    monkeypatch.delenv("BLUE_SERVICE_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert shell.project_root() == tmp_path.resolve()


def test_resolve_workdir_defaults_to_project_root(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """workdir 缺省时落在项目根，而不是服务目录。"""
    install_infra(tmp_path)
    assert shell.resolve_workdir(shell.ShellConfig()) == tmp_path.resolve()


def test_resolve_workdir_relative_to_project_root(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """相对 workdir 按项目根解析。"""
    install_infra(tmp_path)
    config = shell.ShellConfig(workdir="packages/app")
    assert shell.resolve_workdir(config) == (tmp_path / "packages/app").resolve()


def test_resolve_workdir_accepts_absolute(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """绝对 workdir 不受项目根影响。"""
    install_infra(tmp_path / "root")
    target = tmp_path / "elsewhere"
    config = shell.ShellConfig(workdir=str(target))
    assert shell.resolve_workdir(config) == target.resolve()


def test_run_shell_uses_configured_default_workdir(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """configure() 之后默认命令在项目根执行。"""
    install_infra(tmp_path)
    shell.configure(shell.ShellConfig())

    result = asyncio.run(run_shell("pwd"))

    assert result.cwd == str(tmp_path.resolve())
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_run_shell_defaults_to_project_root_without_configure(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """未调用 configure() 时同样回退到项目根。"""
    install_infra(tmp_path)

    result = asyncio.run(run_shell("pwd"))

    assert result.cwd == str(tmp_path.resolve())


def test_run_shell_explicit_cwd_overrides_default(
    install_infra: Callable[..., None], tmp_path: Path
) -> None:
    """显式 cwd 参数优先于服务默认工作目录。"""
    project, explicit = tmp_path / "project", tmp_path / "explicit"
    project.mkdir()
    explicit.mkdir()
    install_infra(project)
    shell.configure(shell.ShellConfig())

    result = asyncio.run(run_shell("pwd", cwd=str(explicit)))

    assert result.cwd == str(explicit.resolve())
