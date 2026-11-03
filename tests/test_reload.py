"""Supervisor 配置热重载测试：服务配置变化触发重写与重启。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from blue.config import load_desired
from blue.control.logs import LogManager
from blue.control.model import ServiceStatus
from blue.control.reconciler import Supervisor
from blue.control.runner import ProcessRunner
from blue.control.state import StateStore

FIXTURE = Path(__file__).parent / "fixtures" / "fake_service.py"


def _write_workspace(root: Path, value: str) -> Path:
    service_dir = root / "services" / "demo"
    service_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(FIXTURE), "--mode", "sleep"]
    rendered = ", ".join(f'"{item}"' for item in command)
    (service_dir / "service.infd").write_text(
        f"""
~ServiceTemplate {{
  value: str = "default"
}}

service {{
  name = "demo"
  kind = "tool"
  package = "demo"
  module = "demo"
  command = [{rendered}]
  depends_on = []
  restart = "always"
  stop_timeout = 2.0
  startup_timeout = 2.0
  readiness = "process"
}}
""",
        encoding="utf-8",
    )
    config = root / "blue.infd"
    config.write_text(
        f"""
!from p"services/demo/service.infd" import ServiceTemplate as DemoService

supervisor {{
  state_dir: str = ".blue"
  reconcile_interval: <float, positive> = 0.05
  restart_backoff: <float, positive> = 0.02
  restart_backoff_max: <float, positive> = 0.1
}}

demo = DemoService(value = "{value}")
""",
        encoding="utf-8",
    )
    return config


async def _wait_ready(state: StateStore, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snapshot = state.read_state()
        if (
            snapshot is not None
            and snapshot.services.get("demo") is not None
            and snapshot.services["demo"].status is ServiceStatus.READY
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("服务未在超时内就绪")


def test_reload_rewrites_config_and_restarts(tmp_path: Path) -> None:
    """修改 blue.infd 后 reload 会重写运行时配置并重启服务。"""
    asyncio.run(_reload(tmp_path))


async def _reload(tmp_path: Path) -> None:
    config = _write_workspace(tmp_path, "a")
    desired = load_desired(config)
    state = StateStore(desired.supervisor.state_dir)
    logs = LogManager(state.log_dir)
    runner = ProcessRunner(logs, uv_frozen=False)
    supervisor = Supervisor(
        config_path=config,
        desired=desired,
        state=state,
        logs=logs,
        runner=runner,
    )
    await supervisor.start()
    try:
        await _wait_ready(state)
        runtime_config = tmp_path / "services" / "demo" / "service_runtime_config.json"
        assert '"a"' in runtime_config.read_text(encoding="utf-8")

        _write_workspace(tmp_path, "b")
        assert await supervisor.reload() is True
        await _wait_ready(state)
        assert '"b"' in runtime_config.read_text(encoding="utf-8")
    finally:
        await supervisor.stop()
