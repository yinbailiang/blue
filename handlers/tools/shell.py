"""外置工具处理器：执行 shell 命令。"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from handlers.support import run_tool

MAX_OUTPUT_CHARS = 20_000
"""stdout / stderr 各自保留的最大字符数。"""


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
    cwd: Annotated[str | None, "工作目录；缺省为工具进程的当前目录"] = None,
    timeout: Annotated[float, Field(gt=0), "超时秒数"] = 30.0,
) -> ShellResult:
    """通过系统 shell 执行命令，返回退出码与输出。

    命令以工具进程的权限运行，没有额外沙箱；超时会连同子进程一并强制结束。
    """
    if not command.strip():
        raise ValueError("command 不能为空")

    workdir = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
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


if __name__ == "__main__":
    raise SystemExit(run_tool(TOOL, "run_shell"))
