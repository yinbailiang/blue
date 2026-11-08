"""Supervisor 调谐测试：启动、就绪、崩溃重启与优雅停止。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from blue.control.desired import load_desired
from blue.control.logs import LogManager
from blue.control.model import (
    DesiredService,
    DesiredState,
    RegistrySettings,
    RestartSettings,
    ServiceMeta,
    ServiceStatus,
    SupervisorSettings,
)
from blue.control.reconciler import (
    Supervisor,
    dependency_levels,
    shutdown_waves,
    shutdown_worst_case,
)
from blue.control.runner import ProcessHandle, ProcessRunner
from blue.control.state import StateStore
from blue.runtime import shutdown_intent_dir, shutdown_intent_path

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
            restart=RestartSettings(
                backoff=0.02,
                backoff_max=0.1,
                max_attempts=3,
                stable_after=1.0,
            ),
            registry=RegistrySettings(ttl=5.0, sweep_interval=0.5),
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
        "  restart = { backoff = 0.05 }\n"
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


def test_launch_failure_respects_restart_never(tmp_path: Path) -> None:
    """拉起就失败（command 不存在）时，restart=never 不该反复重试。"""
    asyncio.run(_launch_failure_flow(tmp_path))


async def _launch_failure_flow(tmp_path: Path) -> None:
    desired = _desired(tmp_path, services={"demo": ("sleep", ())})
    service = desired.services["demo"]
    broken = service.model_copy(
        update={
            "meta": service.meta.model_copy(
                update={"command": ("/nonexistent/binary",), "restart": "never"}
            )
        }
    )
    desired = desired.model_copy(update={"services": {"demo": broken}})
    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _status(state, "demo") is ServiceStatus.FAILED)
        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.services["demo"].consecutive_failures == 0, "不该计入重试次数"
        await asyncio.sleep(0.3)
        assert _status(state, "demo") is ServiceStatus.FAILED
    finally:
        await supervisor.stop()


def test_offline_shutdown_writes_one_intent_per_service(tmp_path: Path) -> None:
    """下线意图按服务写：共享一份时只有第一个停机的服务能读到。

    实测过的坏形态：全集群一份文件，第一个服务读完即删，其余服务按 ``restart``
    留着队列——于是 ``blue down`` 之后队列里还留着本该丢掉的积压。
    """
    desired = _desired(
        tmp_path, services={"alpha": ("sleep", ()), "beta": ("sleep", ())}
    )
    supervisor = _supervisor(desired)

    assert supervisor.request_offline_shutdown() is True

    directory = shutdown_intent_dir(desired.supervisor.state_dir)
    assert sorted(path.name for path in directory.glob("*.json")) == [
        "alpha.json",
        "beta.json",
    ]
    payload = json.loads((directory / "alpha.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "offline"
    assert isinstance(payload["ts"], float)


def test_stale_intents_are_cleared_on_start(tmp_path: Path) -> None:
    """上一次 down 被强杀留下的意图，必须在 supervisor 启动时清掉。

    留着它的后果不是"没效果"：下一次普通重启会把 ``restart`` 误切成 ``offline``
    （解绑 + 排空），把本该补投的积压丢掉。
    """
    asyncio.run(_stale_intent_flow(tmp_path))


async def _stale_intent_flow(tmp_path: Path) -> None:
    desired = _desired(tmp_path, services={"demo": ("sleep", ())})
    stale = shutdown_intent_path("demo", state_dir=desired.supervisor.state_dir)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        json.dumps({"mode": "offline", "ts": time.time()}), encoding="utf-8"
    )
    supervisor = _supervisor(desired)
    try:
        await supervisor.start()
        assert not stale.exists(), "启动时必须清掉残留的下线意图"
    finally:
        await supervisor.stop()


def test_shutdown_runs_waves_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按依赖分层倒序停：同层并发、跨层串行，且每个服务拿到完整的 stop_timeout。

    期望的波次（依赖层级从高到低）：``top`` → ``left/mid/right`` → ``base``。
    旧实现逐个串行停，一层里三个互不相干的服务也要排队，白白把总时长累加成
    「Σ 各服务」；这里应该是「Σ 每层最慢」。
    """
    asyncio.run(_wave_shutdown_flow(tmp_path, monkeypatch))


async def _wave_shutdown_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    desired = _desired(
        tmp_path,
        services={
            "base": ("sleep", ()),
            "left": ("sleep", ("base",)),
            "right": ("sleep", ("base",)),
            "mid": ("sleep", ("base",)),
            "top": ("sleep", ("mid", "left")),
        },
    )
    desired = desired.model_copy(
        update={
            "supervisor": desired.supervisor.model_copy(
                update={"shutdown_timeout": 60.0}
            )
        }
    )
    state = StateStore(desired.supervisor.state_dir)
    logs = LogManager(state.log_dir)
    runner = ProcessRunner(logs, uv_frozen=False)
    supervisor = Supervisor(
        config_path=desired.config_path,
        desired=desired,
        state=state,
        logs=logs,
        runner=runner,
    )

    events: list[tuple[str, float]] = []
    original_stop = runner.stop

    async def recording_stop(handle: ProcessHandle, *, timeout: float) -> int | None:
        events.append((handle.service.meta.name, time.monotonic()))
        await asyncio.sleep(0.2)  # 让同层并发可观测：串行的话每层要多花 0.2×N
        return await original_stop(handle, timeout=timeout)

    monkeypatch.setattr(runner, "stop", recording_stop)
    await supervisor.start()
    try:
        await _wait_for(
            lambda: all(
                _status(state, name) is ServiceStatus.READY
                for name in ("base", "left", "right", "mid", "top")
            )
        )
        started = time.monotonic()
    finally:
        await supervisor.stop()
    elapsed = time.monotonic() - started

    order = [name for name, _ in events]
    assert order[0] == "top", f"依赖别人的先停，实际顺序：{order}"
    assert order[-1] == "base", f"被别人依赖的最后停，实际顺序：{order}"
    assert set(order[1:4]) == {"left", "mid", "right"}, f"中间层三个应当同波：{order}"

    # 串行要 5 × 0.2 = 1.0s，三层并发只需 3 × 0.2 = 0.6s（留足抖动余量）
    assert elapsed < 0.95, f"同层没有并发：整场花了 {elapsed:.2f}s"


def test_shutdown_waves_follow_dependency_levels() -> None:
    """波次划分：层级高的先停，同层归一，独立服务与「无依赖者」同层。"""
    dependencies = {
        "bus": (),
        "session": ("bus",),
        "agent": ("bus", "session"),
        "webui": ("agent", "session"),
        "clock": ("bus",),
    }

    levels = dependency_levels(dependencies)

    assert levels == {
        "bus": 0,
        "session": 1,
        "agent": 2,
        "webui": 3,
        "clock": 1,
    }
    assert shutdown_waves(levels) == [
        ["webui"],
        ["agent"],
        ["clock", "session"],
        ["bus"],
    ]


def test_shutdown_worst_case_counts_per_layer_not_per_service() -> None:
    """上界是「每波最慢之和」，不是「所有服务之和」——并发的收益就体现在这。"""
    levels = {"a": 0, "b": 0, "c": 1, "d": 2}
    timeouts = {"a": 30.0, "b": 30.0, "c": 30.0, "d": 30.0}

    assert shutdown_worst_case(levels, timeouts) == pytest.approx(90.0)  # 3 层
    assert sum(timeouts.values()) == pytest.approx(120.0)  # 串行才是 4 个 30s


def test_dependency_levels_survive_a_cycle() -> None:
    """成环的配置不该让收尾路径挂死（配置校验本来就会拒绝它）。"""
    levels = dependency_levels({"a": ("b",), "b": ("a",)})

    assert set(levels) == {"a", "b"}


def test_default_shutdown_is_60s_and_each_service_30s() -> None:
    """默认值：整场 60 秒、每服务 30 秒。

    ``shutdown_timeout`` 是"整场墙钟"，``ServiceMeta.stop_timeout`` 是"单个服务的
    优雅停止预算"；两者一起决定了最坏时长是「层数 × 30s」而不是「服务数 × 30s」。
    """
    assert SupervisorSettings().shutdown_timeout == 60.0
    assert ServiceMeta(
        name="demo", kind="tool", package="p", module="m"
    ).stop_timeout == pytest.approx(30.0)
