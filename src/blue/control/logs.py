"""服务日志：按大小轮转、时间戳前缀、tail/follow。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["LogManager"]


class LogManager:
    """管理 ``.blue/logs/<service>.log`` 的写入、轮转与读取。"""

    def __init__(
        self,
        log_dir: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须 > 0")
        if backups < 0:
            raise ValueError("backups 不能为负")
        self._log_dir = log_dir
        self._max_bytes = max_bytes
        self._backups = backups
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def log_dir(self) -> Path:
        """日志目录。"""
        return self._log_dir

    def path_for(self, service: str) -> Path:
        """返回服务日志路径。"""
        return self._log_dir / f"{service}.log"

    async def append(self, service: str, stream: str, text: str) -> None:
        """追加一行/一段输出；必要时先轮转。"""
        if not text:
            return
        lock = self._locks.setdefault(service, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._append_sync, service, stream, text)

    def _append_sync(self, service: str, stream: str, text: str) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(service)
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        payload = text if text.endswith("\n") else f"{text}\n"
        rendered = "".join(
            f"{timestamp} [{stream}] {line}\n"
            for line in payload.rstrip("\n").split("\n")
        )
        encoded = rendered.encode("utf-8")
        self._rotate(path, len(encoded))
        with path.open("ab") as handle:
            handle.write(encoded)

    def _rotate(self, path: Path, incoming: int) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size + incoming <= self._max_bytes:
            return
        if self._backups == 0:
            path.unlink(missing_ok=True)
            return
        oldest = path.with_name(f"{path.name}.{self._backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self._backups - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.1"))

    def tail(self, service: str, lines: int = 100) -> list[str]:
        """读取日志末尾 ``lines`` 行。"""
        path = self.path_for(service)
        if not path.is_file():
            return []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return []
            block = min(size, 64 * 1024)
            data = b""
            while block > 0:
                handle.seek(max(0, size - block))
                data = handle.read(block)
                if data.count(b"\n") > lines or block == size:
                    break
                block = min(size, block * 2)
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-lines:]

    async def follow(self, service: str) -> AsyncIterator[str]:
        """从文件末尾持续读取新行。"""
        path = self.path_for(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    yield line.rstrip("\n")
                else:
                    await asyncio.sleep(0.2)
