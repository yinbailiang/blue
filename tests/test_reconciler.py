"""Supervisor 调谐测试：启动、就绪、崩溃重启与优雅停止。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from blue.control.desired import load_desired
from blue.control.logs import LogManager
from blue.control.model import (
    DesiredService,
    DesiredState,
    ServiceMeta,
    ServiceStatus,
    SupervisorSettings,
)
from blue.control.reconciler import Supervisor, build_service_env
from blue.control.runner import ProcessRunner
from blue.control.state import StateStore

FIXTURE = Path(__file__).parent / "fixtures" / "fake_service.py"


def _desired(
    tmp_path: Path,
    *,
    services: dict[str, tuple[str, tuple[str, ...]]],
) -> DesiredState:
    desired_services: dict[str, DesiredService] = {}
    for name, (mode, deps) in services.items():
        service_dir = tmp_path / name
        service_dir.mkdir(parents=True, exist_ok=True)
        config_path = service_dir / "service_runtime_config.json"
        config_path.write_text("{}", encoding="utf-8")
        desired_services[name] = DesiredService(
            meta=ServiceMeta(
                name=name,
                kind="tool",
                package=name,
                module=name,
                command=(sys.executable, str(FIXTURE), "--mode", mode),
                depends_on=deps,
                restart="always",
                stop_timeout=2.0,
                startup_timeout=2.0,
                readiness="process",
            ),
            config={},
            config_hash=f"hash-{name}",
            service_dir=service_dir,
            config_path=config_path,
        )
    return DesiredState(
        generation="gen-1",
        config_path=tmp_path / "blue.infd",
        base_dir=tmp_path,
        supervisor=SupervisorSettings(
            state_dir=tmp_path / ".blue",
            reconcile_interval=0.05,
            restart_backoff=0.02,
            restart_backoff_max=0.1,
            restart_max_attempts=3,
            stable_after=1.0,
            registry_ttl=5.0,
            registry_sweep_interval=0.5,
        ),
        services=desired_services,
        bus_url="amqp://guest:guest@127.0.0.1:5672/",
        bus_transport="rabbit",
    )


def _supervisor(desired: DesiredState) -> Supervisor:
    state = StateStore(desired.supervisor.state_dir)
    logs = LogManager(state.log_dir)
    runner = ProcessRunner(logs, uv_frozen=False)
    return Supervisor(
        config_path=desired.config_path,
        desired=desired,
        state=state,
        logs=logs,
        runner=runner,
    )


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


def _status(state: StateStore, name: str) -> ServiceStatus | None:
    snapshot = state.read_state()
    if snapshot is None:
        return None
    service = snapshot.services.get(name)
    return service.status if service is not None else None


def _restarts(state: StateStore, name: str) -> int:
    snapshot = state.read_state()
    if snapshot is None:
        return 0
    service = snapshot.services.get(name)
    return service.restarts if service is not None else 0


def test_supervisor_starts_and_stops_service(tmp_path: Path) -> None:
    """服务启动到 Ready，停止后进程消失。"""
    asyncio.run(_start_stop(tmp_path))


async def _start_stop(tmp_path: Path) -> None:
    desired = _desired(tmp_path, services={"demo": ("sleep", ())})
    supervisor = _supervisor(desired)
    await supervisor.start()
    state = StateStore(desired.supervisor.state_dir)
    pid: int | None = None
    try:
        await _wait_for(lambda: _status(state, "demo") is ServiceStatus.READY)
        snapshot = state.read_state()
        assert snapshot is not None
        pid = snapshot.services["demo"].pid
        assert pid is not None
    finally:
        await supervisor.stop()

    assert pid is not None
    await _wait_for(lambda: not StateStore.pid_alive(pid), timeout=3.0)


def test_supervisor_restarts_crashed_service(tmp_path: Path) -> None:
    """非零退出后按策略重启。"""
    asyncio.run(_restart(tmp_path))


async def _restart(tmp_path: Path) -> None:
    desired = _desired(tmp_path, services={"demo": ("exit1", ())})
    supervisor = _supervisor(desired)
    await supervisor.start()
    state = StateStore(desired.supervisor.state_dir)
    try:
        await _wait_for(lambda: _restarts(state, "demo") >= 1)
    finally:
        await supervisor.stop()


def test_supervisor_waits_for_dependency(tmp_path: Path) -> None:
    """依赖未就绪时，下游服务保持 Pending。"""
    asyncio.run(_dependency(tmp_path))


def _package_workspace(tmp_path: Path, *, version: str) -> Path:
    """最小可启动 workspace：fake_service 服务 + 带版本号的 pyproject.toml。"""
    service_dir = tmp_path / "services" / "echo"
    service_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(FIXTURE), "--mode", "sleep"]
    manifest = (
        "~ServiceTemplate {\n}\n\n"
        "service {\n"
        '  name = "echo"\n'
        '  kind = "tool"\n'
        '  package = "demo-echo"\n'
        '  module = "demo_echo"\n'
        f"  command = {json.dumps(command)}\n"
        "  depends_on = []\n"
        '  restart = "always"\n'
        "  stop_timeout = 2.0\n"
        "  startup_timeout = 5.0\n"
        '  readiness = "process"\n'
        "}\n"
    )
    (service_dir / "service.infd").write_text(manifest, encoding="utf-8")
    (service_dir / "pyproject.toml").write_text(
        f'[project]\nname = "demo-echo"\nversion = "{version}"\n', encoding="utf-8"
    )
    source = service_dir / "src" / "demo_echo"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = tmp_path / "blue.infd"
    config.write_text(
        '!from p"services/echo/service.infd" import ServiceTemplate as EchoService\n'
        "\n"
        "supervisor {\n"
        '  state_dir: str = ".blue"\n'
        "  reconcile_interval: <float, positive> = 0.05\n"
        "  restart_backoff: <float, positive> = 0.05\n"
        "  shutdown_timeout: <float, positive> = 5.0\n"
        "}\n"
        "\n"
        "echo = EchoService()\n",
        encoding="utf-8",
    )
    return config


def _current_pid(state: StateStore, name: str) -> int | None:
    snapshot = state.read_state()
    if snapshot is None:
        return None
    service = snapshot.services.get(name)
    return service.pid if service is not None else None


def test_supervisor_restarts_when_package_version_changes(tmp_path: Path) -> None:
    """显式 reload 时，服务包版本变化会重启进程。"""
    asyncio.run(_package_version_reload(tmp_path))


async def _package_version_reload(tmp_path: Path) -> None:
    config = _package_workspace(tmp_path, version="0.1.0")
    desired = load_desired(config)
    assert desired.services["echo"].package_version == "0.1.0"

    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    await supervisor.start()
    try:
        await _wait_for(lambda: _status(state, "echo") is ServiceStatus.READY)
        first_pid = _current_pid(state, "echo")
        assert first_pid is not None

        (tmp_path / "services" / "echo" / "pyproject.toml").write_text(
            '[project]\nname = "demo-echo"\nversion = "0.2.0"\n', encoding="utf-8"
        )
        assert await supervisor.reload() is True

        await _wait_for(
            lambda: (
                _current_pid(state, "echo") not in (None, first_pid)
                and _status(state, "echo") is ServiceStatus.READY
            ),
            timeout=20.0,
        )
    finally:
        await supervisor.stop()

    assert supervisor.desired.services["echo"].package_version == "0.2.0"


def test_supervisor_restarts_when_source_changes(tmp_path: Path) -> None:
    """源码变化不会自动重启，显式 reload 时才会扫描并重启。"""
    asyncio.run(_source_change_reload(tmp_path))


async def _source_change_reload(tmp_path: Path) -> None:
    config = _package_workspace(tmp_path, version="0.1.0")
    desired = load_desired(config)
    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    await supervisor.start()
    try:
        await _wait_for(lambda: _status(state, "echo") is ServiceStatus.READY)
        first_pid = _current_pid(state, "echo")
        assert first_pid is not None

        module = tmp_path / "services" / "echo" / "src" / "demo_echo" / "__init__.py"
        module.write_text("VALUE = 2000\n# 改动但未升版本\n", encoding="utf-8")

        # 没有 apply/reload 时，调谐循环不应读取源码或重启服务。
        await asyncio.sleep(0.2)
        assert _current_pid(state, "echo") == first_pid
        assert _status(state, "echo") is ServiceStatus.READY

        assert await supervisor.reload() is True
        await _wait_for(
            lambda: (
                _current_pid(state, "echo") not in (None, first_pid)
                and _status(state, "echo") is ServiceStatus.READY
            ),
            timeout=20.0,
        )
    finally:
        await supervisor.stop()

    assert supervisor.desired.services["echo"].package_version == "0.1.0"


def test_build_service_env_injects_project_root(tmp_path: Path) -> None:
    """服务进程收到项目根，供工具解析相对路径。"""
    desired = _desired(tmp_path, services={"echo": ("ok", ())})
    service = desired.services["echo"]

    env = build_service_env(desired, service, log_dir=tmp_path / "logs")

    assert env["BLUE_PROJECT_ROOT"] == str(tmp_path)
    assert env["BLUE_STATE_DIR"] == str(tmp_path / ".blue")
    assert env["BLUE_LOG_DIR"] == str(tmp_path / "logs")
    assert env["BLUE_SERVICE_KIND"] == "tool"


async def _dependency(tmp_path: Path) -> None:
    desired = _desired(
        tmp_path,
        services={
            "base": ("sleep", ()),
            "child": ("sleep", ("base",)),
        },
    )
    supervisor = _supervisor(desired)
    await supervisor.start()
    state = StateStore(desired.supervisor.state_dir)
    try:
        await _wait_for(lambda: _status(state, "base") is ServiceStatus.READY)
        await _wait_for(lambda: _status(state, "child") is ServiceStatus.READY)
    finally:
        await supervisor.stop()
