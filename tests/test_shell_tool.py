"""shell 工具测试：执行、退出码、超时与输出截断。"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest
from handlers.tools.shell import MAX_OUTPUT_CHARS, run_shell


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
