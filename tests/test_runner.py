"""进程管理与日志测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from blue.control.logs import LogManager
from blue.control.model import DesiredService, ServiceMeta
from blue.control.runner import ProcessRunner, build_command
from blue.errors import SupervisorError

FIXTURE = Path(__file__).parent / "fixtures" / "fake_service.py"


def _desired(tmp_path: Path, mode: str) -> DesiredService:
    service_dir = tmp_path / "service"
    service_dir.mkdir(parents=True, exist_ok=True)
    config_path = service_dir / "service_runtime_config.json"
    config_path.write_text(
        json.dumps(
            {
                "config": {},
                "infra": {
                    "project_root": str(tmp_path),
                    "bus_url": "amqp://guest:guest@127.0.0.1:5672/",
                    "state_dir": str(tmp_path / ".blue"),
                },
            }
        ),
        encoding="utf-8",
    )
    return DesiredService(
        meta=ServiceMeta(
            name="demo",
            kind="tool",
            package="demo",
            module="demo",
            command=(sys.executable, str(FIXTURE), "--mode", mode),
            readiness="process",
        ),
        config={},
        config_hash="hash",
        service_dir=service_dir,
        config_path=config_path,
    )


def test_build_command_prefers_explicit_command() -> None:
    """显式 command 优先于 uv 默认命令。"""
    meta = ServiceMeta(
        name="demo",
        kind="tool",
        package="demo",
        module="demo",
        command=("python", "-m", "demo"),
    )
    assert build_command(meta) == ["python", "-m", "demo"]


def test_runner_writes_logs_and_stops(tmp_path: Path) -> None:
    """进程输出进入日志，停止后进程退出。"""
    asyncio.run(_runner_scenario(tmp_path))


async def _runner_scenario(tmp_path: Path) -> None:
    logs = LogManager(tmp_path / "logs", max_bytes=10_000, backups=1)
    runner = ProcessRunner(logs, uv_frozen=False)
    service = _desired(tmp_path, "sleep")

    handle = await runner.start(
        service,
        instance_id="demo-test",
    )
    try:
        await asyncio.sleep(0.5)
        assert handle.alive is True
    finally:
        exit_code = await runner.stop(handle, timeout=2.0)
    assert exit_code is not None
    assert handle.alive is False

    text = logs.path_for("demo").read_text(encoding="utf-8")
    assert "start name=demo" in text
    assert "[stdout]" in text


def test_runner_requires_runtime_config(tmp_path: Path) -> None:
    """缺少运行时配置时拒绝启动。"""
    logs = LogManager(tmp_path / "logs")
    runner = ProcessRunner(logs)
    service = _desired(tmp_path, "sleep")
    service.config_path.unlink()

    with pytest.raises(SupervisorError, match="运行时配置"):
        asyncio.run(runner.start(service, instance_id="demo-test"))
