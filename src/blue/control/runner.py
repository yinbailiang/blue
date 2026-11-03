"""服务进程管理：启动、日志泵、进程组信号与优雅停止。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

from blue.control.logs import LogManager
from blue.control.model import DesiredService, ServiceMeta
from blue.control.process import signal_tree
from blue.errors import SupervisorError

__all__ = ["ProcessHandle", "ProcessRunner", "build_command"]


@dataclass
class ProcessHandle:
    """一个正在运行的服务进程。"""

    service: DesiredService
    process: asyncio.subprocess.Process
    instance_id: str
    started_at: float
    log_path: Path
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int:
        """进程 PID。"""
        return self.process.pid

    @property
    def pgid(self) -> int:
        """进程组 ID；进程已退出时退化为 PID。"""
        try:
            return os.getpgid(self.process.pid)
        except ProcessLookupError:
            return self.process.pid

    @property
    def alive(self) -> bool:
        """进程是否仍在运行。"""
        return self.process.returncode is None


class ProcessRunner:
    """以独立进程组启动服务，并把 stdout/stderr 落到日志文件。"""

    def __init__(
        self,
        logs: LogManager,
        *,
        uv_frozen: bool = True,
    ) -> None:
        self._logs = logs
        self._uv_frozen = uv_frozen

    @property
    def logs(self) -> LogManager:
        """日志管理器。"""
        return self._logs

    async def start(
        self,
        service: DesiredService,
        *,
        instance_id: str,
        env: dict[str, str],
    ) -> ProcessHandle:
        """启动一个服务进程。

        :raises SupervisorError: 命令不可执行或运行时配置缺失。
        """
        if not service.config_path.is_file():
            raise SupervisorError(
                f"服务 {service.meta.name!r} 缺少运行时配置：{service.config_path}"
            )

        command = build_command(service.meta, uv_frozen=self._uv_frozen)
        merged_env = dict(os.environ)
        merged_env.update(service.meta.env)
        merged_env.update(env)
        merged_env["BLUE_SERVICE_NAME"] = service.meta.name
        merged_env["BLUE_INSTANCE_ID"] = instance_id
        merged_env["BLUE_CONFIG_FILE"] = str(service.config_path)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=service.service_dir,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise SupervisorError(
                f"启动服务 {service.meta.name!r} 失败：{error}"
            ) from error

        handle = ProcessHandle(
            service=service,
            process=process,
            instance_id=instance_id,
            started_at=asyncio.get_running_loop().time(),
            log_path=self._logs.path_for(service.meta.name),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        handle.stdout_task = asyncio.create_task(
            self._pump(service.meta.name, "stdout", process.stdout),
            name=f"log.{service.meta.name}.stdout",
        )
        handle.stderr_task = asyncio.create_task(
            self._pump(service.meta.name, "stderr", process.stderr),
            name=f"log.{service.meta.name}.stderr",
        )
        return handle

    async def stop(self, handle: ProcessHandle, *, timeout: float) -> int | None:
        """优雅停止进程组：SIGTERM → 等待 → SIGKILL。"""
        if handle.process.returncode is None:
            signal_tree(handle.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=timeout)
            except TimeoutError:
                signal_tree(handle.process.pid, signal.SIGKILL)
                await handle.process.wait()
        await self._drain(handle)
        return handle.process.returncode

    async def wait(self, handle: ProcessHandle) -> int:
        """等待进程退出并收尾日志泵。"""
        exit_code = await handle.process.wait()
        await self._drain(handle)
        return exit_code

    async def _drain(self, handle: ProcessHandle) -> None:
        tasks = [
            task
            for task in (handle.stdout_task, handle.stderr_task)
            if task is not None
        ]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=3.0
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump(
        self, service: str, stream_name: str, stream: asyncio.StreamReader
    ) -> None:
        buffer = ""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                await self._logs.append(service, stream_name, line)
        if buffer:
            await self._logs.append(service, stream_name, buffer)


def build_command(meta: ServiceMeta, *, uv_frozen: bool = True) -> list[str]:
    """构造服务启动命令。

    优先使用元数据里的显式 ``command``；否则使用 uv workspace：
    ``uv run [--frozen] --package <package> python -m <module>``。
    """
    if meta.command:
        return list(meta.command)

    uv = shutil.which("uv")
    if uv is None:
        raise SupervisorError(
            f"未找到 uv，无法启动服务 {meta.name!r}；"
            "请安装 uv 或在 service.infd 中提供 command"
        )
    command = [uv, "run"]
    if uv_frozen:
        command.append("--frozen")
    command.extend(["--package", meta.package, "python", "-m", meta.module])
    return command
