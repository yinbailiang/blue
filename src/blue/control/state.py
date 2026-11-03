"""supervisor 状态：单实例锁、state.json、日志目录与孤儿进程清理。"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, ConfigDict, ValidationError

from blue.control.model import ActualState
from blue.control.process import signal_tree
from blue.errors import SupervisorError

__all__ = ["LockInfo", "StateStore"]


class LockInfo(BaseModel):
    """supervisor 锁文件内容。"""

    model_config = ConfigDict(extra="forbid")

    pid: int
    started_at: float
    generation: str


class StateStore:
    """维护 ``.blue/`` 下的锁、状态文件与日志目录。"""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_path = state_dir / "state.json"
        self._lock_path = state_dir / "supervisor.lock"
        self._log_dir = state_dir / "logs"
        self._lock_handle: TextIO | None = None

    @property
    def state_dir(self) -> Path:
        """状态目录。"""
        return self._state_dir

    @property
    def state_path(self) -> Path:
        """状态文件。"""
        return self._state_path

    @property
    def lock_path(self) -> Path:
        """单实例锁文件。"""
        return self._lock_path

    @property
    def log_dir(self) -> Path:
        """日志目录。"""
        return self._log_dir

    def ensure_dirs(self) -> None:
        """创建并收紧状态目录与日志目录权限。"""
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self._state_dir.chmod(0o700)
        with suppress(OSError):
            self._log_dir.chmod(0o700)

    def acquire(self, *, generation: str) -> LockInfo:
        """获取单实例锁并写入 PID 信息。

        :raises SupervisorError: 已有 supervisor 持有锁。
        """
        self.ensure_dirs()
        handle = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            existing = handle.read().strip()
            handle.close()
            raise SupervisorError(
                f"已有 supervisor 正在运行（{self._lock_path}）："
                f"{existing or '未知进程'}"
            ) from error

        info = LockInfo(pid=os.getpid(), started_at=time.time(), generation=generation)
        handle.seek(0)
        handle.truncate()
        handle.write(info.model_dump_json())
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        self._lock_handle = handle
        return info

    def release(self) -> None:
        """释放单实例锁。"""
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            handle.close()

    def read_lock_info(self) -> LockInfo | None:
        """读取锁文件中的 supervisor 信息；文件缺失或损坏返回 ``None``。"""
        if not self._lock_path.is_file():
            return None
        try:
            return LockInfo.model_validate_json(
                self._lock_path.read_text(encoding="utf-8")
            )
        except OSError, ValidationError:
            return None

    @staticmethod
    def pid_alive(pid: int) -> bool:
        """判断 PID 是否仍存在（不保证是目标进程）。"""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def supervisor_running(self) -> tuple[bool, LockInfo | None]:
        """返回 ``(是否有 supervisor 在运行, 锁信息)``。"""
        info = self.read_lock_info()
        if info is None:
            return False, None
        return self.pid_alive(info.pid), info

    def read_state(self) -> ActualState | None:
        """读取上一次写入的实际状态。"""
        if not self._state_path.is_file():
            return None
        try:
            return ActualState.model_validate_json(
                self._state_path.read_text(encoding="utf-8")
            )
        except OSError, ValidationError:
            return None

    def write_state(self, state: ActualState) -> None:
        """原子写入状态快照。"""
        self.ensure_dirs()
        temporary = self._state_path.with_name(f".state.{os.getpid()}.tmp")
        try:
            temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    async def cleanup_orphans(self, previous: ActualState | None) -> list[str]:
        """清理上一次 supervisor 遗留的、可确认身份的子进程。

        只有 ``/proc/<pid>/environ`` 中的 ``BLUE_INSTANCE_ID`` 与记录一致时才
        终止进程组，避免 PID 复用误杀无关进程。
        """
        if previous is None:
            return []

        cleaned: list[str] = []
        for name, service in previous.services.items():
            pid = service.pid
            instance_id = service.instance_id
            if pid is None or instance_id is None:
                continue
            if not self.pid_alive(pid):
                continue
            if not _pid_matches(pid, instance_id):
                continue

            signal_tree(pid, signal.SIGTERM)
            for _ in range(50):
                if not self.pid_alive(pid):
                    break
                await asyncio.sleep(0.1)
            if self.pid_alive(pid):
                signal_tree(pid, signal.SIGKILL)
            cleaned.append(name)
        return cleaned


def _pid_matches(pid: int, instance_id: str) -> bool:
    """校验进程环境中的 ``BLUE_INSTANCE_ID``（仅 Linux 可验证）。"""
    environ = Path(f"/proc/{pid}/environ")
    try:
        raw = environ.read_bytes()
    except OSError:
        return False
    marker = f"BLUE_INSTANCE_ID={instance_id}".encode()
    return marker in raw.split(b"\0")
