"""blue 外置工具：执行 shell 命令。"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from blue.runtime import (
    PROJECT_ROOT_ENV,
    WorkspaceConfig,
    load_runtime_config,
    project_root,
    resolve_workspace,
    run_tool,
)

MAX_OUTPUT_CHARS = 20_000
"""stdout / stderr 各自保留的最大字符数。"""


class ShellConfig(BaseModel):
    """shell 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workdir: str | None = Field(
        default=None,
        description="命令的默认工作目录；缺省为项目根，相对路径按项目根解析",
    )


def resolve_workdir(config: ShellConfig) -> Path:
    """把配置解析为绝对工作目录。

    优先级：显式 ``workdir`` > 项目根 > 进程当前目录。
    """
    return resolve_workspace(WorkspaceConfig(workdir=config.workdir))


_default_workdir: Path | None = None
"""进程级默认工作目录；由 :func:`configure` 注入，未注入时回退项目根。"""


def configure(config: ShellConfig) -> None:
    """设置本进程后续 :func:`run_shell` 调用的默认工作目录。"""
    global _default_workdir
    _default_workdir = resolve_workdir(config)


def default_workdir() -> Path:
    """当前进程的命令默认工作目录。"""
    if _default_workdir is not None:
        return _default_workdir
    return project_root()


__all__ = [
    "CONFIG_MODEL",
    "MAX_OUTPUT_CHARS",
    "PROJECT_ROOT_ENV",
    "ShellConfig",
    "ShellResult",
    "configure",
    "default_workdir",
    "main",
    "project_root",
    "resolve_workdir",
    "run_shell",
]

CONFIG_MODEL = ShellConfig
"""契约测试使用的运行时配置模型。"""


class ShellResult(BaseModel):
    """命令执行结果。"""

    command: str = Field(description="执行的命令")
    cwd: str = Field(description="工作目录（绝对路径）")
    exit_code: int | None = Field(description="退出码；超时强杀时为 null")
    success: bool = Field(description="退出码为 0 且未超时")
    stdout: str = Field(description="标准输出（可能被截断）")
    stderr: str = Field(description="标准错误（可能被截断）")
    timed_out: bool = Field(description="是否因超时被强制结束")
    duration_ms: int = Field(description="执行耗时（毫秒）")


async def run_shell(
    command: Annotated[str, "要执行的 shell 命令"],
    cwd: Annotated[str | None, "工作目录；缺省为项目根目录"] = None,
    timeout: Annotated[float, Field(gt=0), "超时秒数"] = 30.0,
) -> ShellResult:
    """通过系统 shell 执行命令，返回退出码与输出。

    命令以工具进程的权限运行，没有额外沙箱；超时会连同子进程一并强制结束。
    未指定 ``cwd`` 时使用服务配置的默认工作目录（缺省为项目根）。
    """
    if not command.strip():
        raise ValueError("command 不能为空")

    workdir = Path(cwd).expanduser().resolve() if cwd else default_workdir()
    if not workdir.is_dir():
        raise ValueError(f"工作目录不存在：{workdir}")

    started = time.monotonic()
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communicate = asyncio.create_task(process.communicate())
    done, _ = await asyncio.wait({communicate}, timeout=timeout)
    timed_out = communicate not in done
    if timed_out:
        _terminate(process)
    stdout, stderr = await communicate

    duration_ms = int((time.monotonic() - started) * 1000)
    exit_code = None if timed_out else process.returncode
    return ShellResult(
        command=command,
        cwd=str(workdir),
        exit_code=exit_code,
        success=exit_code == 0,
        stdout=_truncate(_decode(stdout)),
        stderr=_truncate(_decode(stderr)),
        timed_out=timed_out,
        duration_ms=duration_ms,
    )


def _terminate(process: asyncio.subprocess.Process) -> None:
    """强制结束命令及其子进程。"""
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return


def _decode(data: bytes) -> str:
    """把子进程输出解码为文本，非法字节按替换字符处理。"""
    return data.decode("utf-8", errors="replace")


def _truncate(text: str) -> str:
    """超长输出保留首尾，中间以省略标记代替。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    omitted = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:half]}\n…（输出过长，已省略 {omitted} 个字符）…\n{text[-half:]}"


TOOL = run_shell
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure(load_runtime_config(ShellConfig))
    return run_tool(TOOL, "shell")


if __name__ == "__main__":
    raise SystemExit(main())
