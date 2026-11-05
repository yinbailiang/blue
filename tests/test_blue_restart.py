"""`scripts/blue_restart.py` 的镜像测试：收敛判定、会话挑选、消息渲染。

脚本本身是"给别人用的操作助手"，但它的判定逻辑决定了 agent 会不会误判
"重启完成"，所以照样要测。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from scripts import blue_restart


def _write_state(
    state_dir: Path,
    *,
    statuses: dict[str, str] | None = None,
    phase: str | None = "stable",
    pid: int | None = None,
    generation_id: int = 7,
    config_error: str | None = None,
    failures: list[str] | None = None,
    updated_at: float | None = None,
) -> Path:
    """写一份 `.blue/state.json`（字段与真实快照一致）。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    services = {
        name: {"name": name, "status": status}
        for name, status in (statuses or {"demo": "Ready"}).items()
    }
    payload: dict[str, object] = {
        "version": 1,
        "generation": "abc123",
        "supervisor_pid": os.getpid() if pid is None else pid,
        "started_at": 1.0,
        "updated_at": time.time() if updated_at is None else updated_at,
        "config_error": config_error,
        "services": services,
        "contracts": {},
        "generation_id": generation_id,
        "last_stable_id": generation_id,
        "activation": (
            {
                "phase": phase,
                "generation_id": generation_id,
                "previous_id": None,
                "started_at": 1.0,
                "deadline": 2.0,
                "message": "",
                "failures": failures or [],
            }
            if phase is not None
            else None
        ),
    }
    path = blue_restart.state_path(state_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_read_state_reports_converged(tmp_path: Path) -> None:
    """全部 Ready + phase stable → 判定为已收敛。"""
    _write_state(tmp_path, statuses={"a": "Ready", "b": "Ready"})

    raw, snapshot = blue_restart.read_state(tmp_path)

    assert raw is not None
    assert snapshot.converged is True
    assert snapshot.total == 2
    assert snapshot.ready == 2
    assert snapshot.generation_id == 7


def test_read_state_detects_not_ready(tmp_path: Path) -> None:
    """有一个没 Ready 就不算收敛，并列出是哪个。"""
    _write_state(tmp_path, statuses={"a": "Ready", "b": "Starting"})

    _, snapshot = blue_restart.read_state(tmp_path)

    assert snapshot.converged is False
    assert snapshot.not_ready == ["b"]


def test_dead_supervisor_is_not_converged(tmp_path: Path) -> None:
    """调谐器进程不在（pid 不存在）就不能报"好"。"""
    _write_state(tmp_path, pid=999_999_999)

    _, snapshot = blue_restart.read_state(tmp_path)

    assert snapshot.alive is False
    assert snapshot.converged is False


def test_missing_state_file_is_ignored(tmp_path: Path) -> None:
    """没有状态文件时返回空快照，不抛异常。"""
    raw, snapshot = blue_restart.read_state(tmp_path / "nope")

    assert raw is None
    assert snapshot.converged is False
    assert snapshot.total == 0


def test_terminal_failure_on_rolled_back(tmp_path: Path) -> None:
    """自动回滚是终态失败：不必等到超时。"""
    _write_state(
        tmp_path,
        statuses={"a": "Ready", "b": "Failed"},
        phase="rolled_back",
        failures=["b"],
    )

    _, snapshot = blue_restart.read_state(tmp_path)

    failure = snapshot.terminal_failure
    assert failure is not None
    assert "rolled_back" in failure and "b" in failure


def test_terminal_failure_on_config_error(tmp_path: Path) -> None:
    """配置错误也算终态失败。"""
    _write_state(tmp_path, config_error="blue.infd 编译失败")

    _, snapshot = blue_restart.read_state(tmp_path)

    failure = snapshot.terminal_failure
    assert failure is not None
    assert "配置错误" in failure


def test_wait_converged_returns_immediately_when_stable(tmp_path: Path) -> None:
    """已收敛就立刻返回，不浪费时间。"""
    _write_state(tmp_path)

    snapshot, converged = blue_restart.wait_converged(
        tmp_path, timeout=5.0, after=0.0, poll=0.01
    )

    assert converged is True
    assert snapshot.converged is True


def test_wait_converged_stops_on_terminal_failure(tmp_path: Path) -> None:
    """终态失败要立刻停，别死等整个 timeout。"""
    _write_state(tmp_path, phase="failed", failures=["a"], statuses={"a": "Failed"})
    started = time.monotonic()

    snapshot, converged = blue_restart.wait_converged(
        tmp_path, timeout=10.0, after=0.0, poll=0.01
    )

    assert converged is False
    assert time.monotonic() - started < 2.0
    assert snapshot.terminal_failure is not None


def test_wait_converged_ignores_stale_snapshot(tmp_path: Path) -> None:
    """`after` 之前的快照不算数：execv 后 PID 不变，只能靠时间戳分辨新旧。"""
    _write_state(tmp_path, updated_at=time.time() - 100)

    snapshot, converged = blue_restart.wait_converged(
        tmp_path, timeout=0.2, after=time.time(), poll=0.05
    )

    assert converged is False
    assert snapshot.updated_at < time.time()


def test_pick_session_returns_most_recent(tmp_path: Path) -> None:
    """挑最近活跃的会话（就是当前这轮对话）。"""
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at REAL,"
            " updated_at REAL, steps INTEGER, message_count INTEGER)"
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, 0, 0)",
            [("old", 1.0, 10.0), ("newest", 2.0, 20.0), ("middle", 3.0, 15.0)],
        )
        connection.commit()
    finally:
        connection.close()

    assert blue_restart.pick_session(tmp_path) == "newest"


def test_pick_session_without_database(tmp_path: Path) -> None:
    """没有会话库时返回 None（只读共享库，不阻断了重启脚本）。"""
    assert blue_restart.pick_session(tmp_path) is None


def test_bus_url_for_reads_generation_config(tmp_path: Path) -> None:
    """总线地址从当前世代的 agent 配置里取。"""
    service_dir = tmp_path / "current" / "services" / "agent"
    service_dir.mkdir(parents=True)
    (service_dir / "service_runtime_config.json").write_text(
        json.dumps({"config": {}, "infra": {"bus_url": "amqp://x/y"}}),
        encoding="utf-8",
    )

    assert blue_restart.bus_url_for(tmp_path) == "amqp://x/y"


def test_bus_url_for_without_config(tmp_path: Path) -> None:
    """当前世代里没有 agent 配置、环境变量也没有时返回 None。"""
    assert blue_restart.bus_url_for(tmp_path) is None


def test_render_message_ok(tmp_path: Path) -> None:
    """成功消息要包含世代号、就绪比例与日志路径。"""
    text = blue_restart.render_message(
        {
            "ok": True,
            "commands": ["apply"],
            "generation_id": 13,
            "phase": "stable",
            "ready": 18,
            "total": 18,
            "elapsed_s": 41.4,
            "log": "/tmp/x.log",
        }
    )

    assert "重启完成" in text
    assert "0013" in text
    assert "18/18 Ready" in text
    assert "/tmp/x.log" in text


def test_render_message_failure_lists_reason(tmp_path: Path) -> None:
    """失败消息要带原因与未就绪服务，别只说"失败"。"""
    text = blue_restart.render_message(
        {
            "ok": False,
            "commands": ["apply"],
            "generation_id": 14,
            "phase": "rolled_back",
            "ready": 17,
            "total": 18,
            "elapsed_s": 120.0,
            "not_ready": ["weather"],
            "detail": "激活rolled_back：weather",
            "timed_out": True,
            "log": "/tmp/y.log",
        }
    )

    assert "重启未收敛" in text
    assert "rolled_back" in text
    assert "weather" in text
    assert "已到等待上限" in text
