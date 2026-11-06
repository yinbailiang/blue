"""状态存储测试：原子读写、单实例锁与锁信息。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blue.control.model import ActualService, ActualState, ServiceStatus
from blue.control.state import MaintenanceLock, StateStore
from blue.errors import SupervisorError


def test_state_roundtrip(tmp_path: Path) -> None:
    """状态快照可原子写入并读回。"""
    store = StateStore(tmp_path / ".blue")
    state = ActualState(
        generation="gen-1",
        supervisor_pid=os.getpid(),
        started_at=1.0,
        updated_at=2.0,
        services={
            "demo": ActualService(
                name="demo",
                kind="tool",
                status=ServiceStatus.READY,
                pid=123,
                instance_id="demo-1",
            )
        },
    )

    store.write_state(state)
    loaded = store.read_state()

    assert loaded is not None
    assert loaded.generation == "gen-1"
    assert loaded.services["demo"].status is ServiceStatus.READY
    assert loaded.services["demo"].pid == 123


def test_acquire_and_release_lock(tmp_path: Path) -> None:
    """获取锁后可读取锁信息，释放后不再持有。"""
    store = StateStore(tmp_path / ".blue")

    info = store.acquire(generation="gen-1")
    try:
        assert info.pid == os.getpid()
        assert store.read_lock_info() is not None
        running, current = store.supervisor_running()
        assert running is True
        assert current is not None
    finally:
        store.release()

    # 同一进程的第二个句柄也不应再持有锁
    second = StateStore(tmp_path / ".blue")
    second.acquire(generation="gen-2")
    second.release()


def test_second_acquire_fails_while_held(tmp_path: Path) -> None:
    """锁被占用时第二个 supervisor 无法启动。"""
    first = StateStore(tmp_path / ".blue")
    second = StateStore(tmp_path / ".blue")
    first.acquire(generation="gen-1")
    try:
        with pytest.raises(SupervisorError, match="已有 supervisor"):
            second.acquire(generation="gen-2")
    finally:
        first.release()


def test_maintenance_lock_is_exclusive(tmp_path: Path) -> None:
    """构建与回收共用一把锁：持有期间别人拿不到，释放后立刻能拿。"""
    first = MaintenanceLock(tmp_path / ".blue")
    second = MaintenanceLock(tmp_path / ".blue")

    assert first.acquire() is True
    assert second.acquire() is False
    assert second.acquire(wait=0.2) is False

    first.release()

    assert second.acquire() is True
    second.release()


def test_maintenance_lock_release_is_idempotent(tmp_path: Path) -> None:
    """没拿到锁 / 重复释放都不该抛——调用方总是写在 ``finally`` 里。"""
    lock = MaintenanceLock(tmp_path / ".blue")

    lock.release()

    assert lock.acquire() is True
    lock.release()
    lock.release()


def test_lock_records_versions(tmp_path: Path) -> None:
    """锁文件记录启动时的 blue 版本与配置格式版本。"""
    store = StateStore(tmp_path / ".blue")

    info = store.acquire(generation="gen-1", blue_version="1.2.3", config_version=7)
    try:
        loaded = store.read_lock_info()
    finally:
        store.release()

    assert info.blue_version == "1.2.3"
    assert info.config_version == 7
    assert loaded is not None
    assert loaded.blue_version == "1.2.3"
    assert loaded.config_version == 7


def test_legacy_lock_without_versions(tmp_path: Path) -> None:
    """旧版锁文件缺少版本字段时按 0/空串读取。"""
    state_dir = tmp_path / ".blue"
    state_dir.mkdir()
    (state_dir / "supervisor.lock").write_text(
        json.dumps({"pid": os.getpid(), "started_at": 1.0, "generation": "gen"}),
        encoding="utf-8",
    )
    store = StateStore(state_dir)

    info = store.read_lock_info()

    assert info is not None
    assert info.blue_version == ""
    assert info.config_version == 0


def test_state_dir_permissions(tmp_path: Path) -> None:
    """状态目录与日志目录权限为 0700。"""
    store = StateStore(tmp_path / ".blue")
    store.ensure_dirs()

    assert stat_mode(store.state_dir) == 0o700
    assert stat_mode(store.log_dir) == 0o700


def stat_mode(path: Path) -> int:
    """取路径权限位。"""
    return path.stat().st_mode & 0o777


def _wait_for_instance_id(pid: int, instance_id: str, *, timeout: float = 5.0) -> None:
    """等子进程的 ``/proc/<pid>/environ`` 里真的出现 ``BLUE_INSTANCE_ID`` 标记。

    ``cleanup_orphans`` 靠这个标记确认进程身份，读不到就静默跳过——于是本用例在
    全量并发跑时会偶发「cleaned 为空」的假失败。
    """
    marker = f"BLUE_INSTANCE_ID={instance_id}".encode()
    environ = Path(f"/proc/{pid}/environ")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if marker in environ.read_bytes().split(b"\0"):
                return
        except OSError:
            pass
        time.sleep(0.02)
    pytest.fail(f"子进程 {pid} 的 BLUE_INSTANCE_ID 在 {timeout}s 内始终不可读")


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="需要 Linux /proc")
def test_cleanup_orphans_kills_matching_instance(tmp_path: Path) -> None:
    """只终止 BLUE_INSTANCE_ID 匹配的遗留进程组。"""
    env = dict(os.environ)
    env["BLUE_INSTANCE_ID"] = "orphan-test-1"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        # Popen 返回时 exec 已完成，但 /proc/<pid>/environ 在高负载下可能短暂为空：
        # 全量并发跑时这里会偶发「匹配不到」→ cleaned 为空。先等标记真的可读。
        _wait_for_instance_id(process.pid, "orphan-test-1")

        store = StateStore(tmp_path / ".blue")
        previous = ActualState(
            generation="old",
            supervisor_pid=os.getpid(),
            started_at=1.0,
            updated_at=2.0,
            services={
                "demo": ActualService(
                    name="demo",
                    kind="tool",
                    status=ServiceStatus.READY,
                    pid=process.pid,
                    pgid=os.getpgid(process.pid),
                    instance_id="orphan-test-1",
                )
            },
        )

        cleaned = asyncio.run(store.cleanup_orphans(previous))

        assert cleaned == ["demo"]
        assert process.wait(timeout=5.0) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)
