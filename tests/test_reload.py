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


def _write_workspace(root: Path, value: str, *, contract_value: str = "a") -> Path:
    contract_dir = root / "contracts" / "demo"
    (contract_dir / "src" / "demo_contracts").mkdir(parents=True, exist_ok=True)
    (contract_dir / "contract.infd").write_text(
        """
contract {
  name = "demo"
  package = "demo-contracts"
  module = "demo_contracts"
  description = "demo contract"
}
""",
        encoding="utf-8",
    )
    (contract_dir / "pyproject.toml").write_text(
        """
[project]
name = "demo-contracts"
version = "0.1.0"
requires-python = ">=3.14"

[build-system]
requires = ["uv_build>=0.9.0,<0.14"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "demo_contracts"
module-root = "src"
""",
        encoding="utf-8",
    )
    (contract_dir / "src" / "demo_contracts" / "__init__.py").write_text(
        f'VALUE = "{contract_value}"\n', encoding="utf-8"
    )

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
  depends_contracts = ["demo"]
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


async def _wait_ready_pid(
    state: StateStore, old_pid: int | None, timeout: float = 5.0
) -> None:
    """等待服务以新 PID 进入 Ready（证明发生了重启）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snapshot = state.read_state()
        service = snapshot.services.get("demo") if snapshot is not None else None
        if (
            service is not None
            and service.status is ServiceStatus.READY
            and service.pid != old_pid
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("服务未在超时内以新 PID 就绪")


def test_reload_rewrites_config_and_restarts(tmp_path: Path) -> None:
    """修改 blue.infd 后 reload 会重写运行时配置并重启服务。"""
    asyncio.run(_reload(tmp_path))


def test_contract_change_restarts_dependent_service(tmp_path: Path) -> None:
    """契约源码变化时，声明 depends_contracts 的服务会被重启。"""
    asyncio.run(_contract_reload(tmp_path))


async def _contract_reload(tmp_path: Path) -> None:
    config = _write_workspace(tmp_path, "a", contract_value="v1")
    before = load_desired(config)
    assert set(before.contracts) == {"demo"}

    state = StateStore(before.supervisor.state_dir)
    logs = LogManager(state.log_dir)
    runner = ProcessRunner(logs, uv_frozen=False)
    supervisor = Supervisor(
        config_path=config,
        desired=before,
        state=state,
        logs=logs,
        runner=runner,
    )
    await supervisor.start()
    try:
        await _wait_ready(state)
        snapshot = state.read_state()
        assert snapshot is not None
        old_pid = snapshot.services["demo"].pid

        _write_workspace(tmp_path, "a", contract_value="v2")
        after = load_desired(config)
        assert (
            after.contracts["demo"].source_hash != before.contracts["demo"].source_hash
        )

        assert await supervisor.reload() is True
        await _wait_ready_pid(state, old_pid)
    finally:
        await supervisor.stop()


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
