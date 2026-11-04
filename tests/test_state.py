"""状态存储测试：原子读写、单实例锁与锁信息。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from blue.control.model import ActualService, ActualState, ServiceStatus
from blue.control.state import StateStore
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
