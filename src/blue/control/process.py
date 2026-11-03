"""进程树信号工具：终止服务时连同其子进程一起处理。"""

from __future__ import annotations

import os
import signal
from pathlib import Path

__all__ = ["signal_tree"]


def signal_tree(pid: int, sig: signal.Signals, *, group: bool = True) -> None:
    """向进程及其全部后代发送信号。

    :param group: 顶层进程是否按进程组发送；递归子进程只按 PID 发送，避免
        误伤与父进程同组的其他进程。
    """
    for child in _children(pid):
        signal_tree(child, sig, group=False)
    if group:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError, PermissionError:
            _signal_pid(pid, sig)
    else:
        _signal_pid(pid, sig)


def _children(pid: int) -> list[int]:
    """读取 Linux ``/proc`` 中的直接子进程列表。"""
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tasks = list(task_dir.iterdir())
    except OSError:
        return []

    found: set[int] = set()
    for task in tasks:
        try:
            raw = (task / "children").read_text(encoding="ascii")
        except OSError:
            continue
        for token in raw.split():
            try:
                child = int(token)
            except ValueError:
                continue
            if child != pid:
                found.add(child)
    return sorted(found)


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError, PermissionError:
        return
