"""世代激活闸门测试：健康验证、超时自动回滚、崩溃后回退。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from blue.control.artifacts import ArtifactStore
from blue.control.generations import GenerationStore
from blue.control.logs import LogManager
from blue.control.model import (
    ActivationState,
    ActualState,
    DesiredService,
    DesiredState,
    ServiceMeta,
    ServiceStatus,
    SupervisorSettings,
)
from blue.control.reconciler import Supervisor
from blue.control.runner import ProcessRunner
from blue.control.state import StateStore

FIXTURE = Path(__file__).parent / "fixtures" / "fake_service.py"


def _desired(
    tmp_path: Path,
    *,
    generation: str,
    mode: str = "sleep",
    activation_timeout: float = 1.0,
    auto_rollback: bool = True,
    ready_settle: float = 0.3,
) -> DesiredState:
    """一个用假服务进程的期望状态（自定义 command，因此不需要产物）。"""
    service_dir = tmp_path / "demo"
    service_dir.mkdir(parents=True, exist_ok=True)
    config_path = service_dir / "service_runtime_config.json"
    config_path.write_text("{}", encoding="utf-8")
    service = DesiredService(
        meta=ServiceMeta(
            name="demo",
            kind="tool",
            package="demo",
            module="demo",
            command=(sys.executable, str(FIXTURE), "--mode", mode),
            restart="always",
            stop_timeout=2.0,
            startup_timeout=2.0,
            readiness="process",
            ready_settle=ready_settle,
        ),
        config={},
        config_hash=f"cfg-{mode}",
        service_dir=service_dir,
        config_path=config_path,
        cwd=tmp_path,
    )
    return DesiredState(
        generation=generation,
        config_path=tmp_path / "blue.infd",
        base_dir=tmp_path,
        supervisor=SupervisorSettings(
            state_dir=tmp_path / ".blue",
            reconcile_interval=0.05,
            restart_backoff=0.02,
            restart_backoff_max=0.1,
            restart_max_attempts=3,
            stable_after=1.0,
            activation_timeout=activation_timeout,
            generation_auto_rollback=auto_rollback,
        ),
        services={"demo": service},
        bus_url="amqp://guest:guest@127.0.0.1:5672/",
        bus_transport="rabbit",
        deps_hash="deps-1",
    )


def _store(tmp_path: Path) -> GenerationStore:
    return GenerationStore(tmp_path / ".blue")


def _create(tmp_path: Path, desired: DesiredState, *, stable: bool) -> int:
    """冻结一个世代，可选标记 stable 并激活。"""
    store = _store(tmp_path)
    artifacts = ArtifactStore(
        tmp_path / ".blue", project_root=tmp_path, warn_cross_device=False
    )
    generation = store.create(desired, artifacts, reason="test")
    if stable:
        store.mark_stable(generation)
    store.activate(generation)
    return generation.id


def _supervisor(desired: DesiredState) -> Supervisor:
    state = StateStore(desired.supervisor.state_dir)
    logs = LogManager(state.log_dir)
    return Supervisor(
        config_path=desired.config_path,
        desired=desired,
        state=state,
        logs=logs,
        runner=ProcessRunner(logs, uv_frozen=False),
    )


async def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


def _activation_phase(state: StateStore) -> str | None:
    snapshot = state.read_state()
    if snapshot is None or snapshot.activation is None:
        return None
    return snapshot.activation.phase


def _status(state: StateStore, name: str = "demo") -> ServiceStatus | None:
    snapshot = state.read_state()
    if snapshot is None:
        return None
    service = snapshot.services.get(name)
    return service.status if service is not None else None


def _config_error(state: StateStore) -> str | None:
    snapshot = state.read_state()
    return snapshot.config_error if snapshot is not None else None


def _generation_id(state: StateStore) -> int | None:
    snapshot = state.read_state()
    return snapshot.generation_id if snapshot is not None else None


def test_activation_marks_generation_stable(tmp_path: Path) -> None:
    """全部服务就绪后落 .stable，并把 activation.phase 置为 stable。"""
    asyncio.run(_stable_flow(tmp_path))


async def _stable_flow(tmp_path: Path) -> None:
    desired = _desired(tmp_path, generation="gen-1")
    generation_id = _create(tmp_path, desired, stable=False)
    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _activation_phase(state) == "stable")
        generation = _store(tmp_path).load(generation_id)
        assert generation.stable is True
        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.generation_id == generation_id
        assert snapshot.last_stable_id == generation_id
    finally:
        await supervisor.stop()


def test_activation_timeout_rolls_back(tmp_path: Path) -> None:
    """新世代起不来（进程一直崩）→ 超时后自动回到上一个 stable 世代。

    这条曾经在并行跑时约一半概率失败（等不到 ``rolled_back``，日志里连 ERROR
    都没有）。根因不在用例：``readiness="process"`` 的就绪探针只观察 100ms，
    一个熬过这 100ms 才崩的进程会被判 READY，激活闸门若恰好在此刻看到「全绿」
    就 mark_stable，坏世代永不回滚。闸门改成要求「全绿连续保持一段宽限」后，
    这条恢复稳定，不再需要 slow 标记绕过。
    """
    asyncio.run(_rollback_flow(tmp_path, auto_rollback=True))


def test_activation_timeout_without_auto_rollback_marks_failed(
    tmp_path: Path,
) -> None:
    """关掉自动回滚时只标记 failed，current 不动。

    与上面共用 ``_rollback_flow``，同样依赖 1 秒激活超时的时序。
    """
    asyncio.run(_rollback_flow(tmp_path, auto_rollback=False))


@pytest.mark.slow
def test_activation_rejects_service_that_dies_right_after_ready(
    tmp_path: Path,
) -> None:
    """服务被就绪探针判成 READY 后立刻崩溃 → 不许标 stable，必须回滚。

    这条守的是**闸门侧的兜底**：即使 ``ready_settle`` 被配得太短（这里故意设成
    0.05 秒），让 ``exit1-late``（熬 150ms 再退出）骗过就绪探针，激活闸门也必须
    靠「全绿连续保持一段宽限」把它判失败。

    没有这层兜底时（宽限置 0），闸门只要看到「此刻全绿」就 ``mark_stable``，
    坏世代从此被当成稳定版本、永不回滚——「激活失败自动回滚」静默失效，
    且不报任何错。这是确定性用例，不依赖机器快慢。
    """
    asyncio.run(_dies_right_after_ready(tmp_path))


async def _dies_right_after_ready(tmp_path: Path) -> None:
    good = _desired(tmp_path, generation="gen-1", activation_timeout=2.0)
    _create(tmp_path, good, stable=True)
    supervisor = _supervisor(good)
    state = StateStore(good.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _status(state) is ServiceStatus.READY)

        broken = _desired(
            tmp_path,
            generation="gen-2",
            mode="exit1-late",
            activation_timeout=2.0,
            # 故意把观察期配得比「起来就崩」的存活时间（150ms）还短，
            # 逼出「探针误判 READY」这个场景，验证闸门能兜住
            ready_settle=0.05,
        )
        second_id = _create(tmp_path, broken, stable=False)
        assert await supervisor.reload() is True

        await _wait_for(lambda: _activation_phase(state) == "rolled_back", timeout=60.0)
        store = _store(tmp_path)
        assert store.load(second_id).failed is True
        current = store.current()
        assert current is not None
        assert current.id != second_id, "坏世代被当成 stable，回滚没有发生"
    finally:
        await supervisor.stop()


async def _rollback_flow(tmp_path: Path, *, auto_rollback: bool) -> None:
    good = _desired(tmp_path, generation="gen-1", auto_rollback=auto_rollback)
    first_id = _create(tmp_path, good, stable=True)
    supervisor = _supervisor(good)
    state = StateStore(good.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _status(state) is ServiceStatus.READY)

        broken = _desired(
            tmp_path,
            generation="gen-2",
            mode="exit1",
            auto_rollback=auto_rollback,
        )
        second_id = _create(tmp_path, broken, stable=False)
        assert await supervisor.reload() is True

        expected = "rolled_back" if auto_rollback else "failed"
        # 上限给到 60 秒：回滚要先 mark_failed 再 _reload_locked 重建整棵运行时，
        # 而 _reload_locked 里要逐个停旧服务、起新服务。全量并发跑（-n 12+）时
        # 每个 python 子进程启动都会被 CPU 争抢拖慢，15 秒不够用——那是机器慢，
        # 不是状态机卡住。断言内容不变，只是别把「机器忙」误判成「功能坏了」。
        await _wait_for(lambda: _activation_phase(state) == expected, timeout=60.0)

        store = _store(tmp_path)
        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.activation is not None
        assert snapshot.activation.generation_id == second_id
        assert store.load(second_id).failed is True
        if auto_rollback:
            current = store.current()
            assert current is not None
            assert current.id == first_id
            assert snapshot.activation.previous_id == first_id
            # 回滚后服务应恢复就绪
            await _wait_for(lambda: _status(state) is ServiceStatus.READY, timeout=60.0)
        else:
            current = store.current()
            assert current is not None
            assert current.id == second_id
    finally:
        await supervisor.stop()


def test_crash_recovery_falls_back_to_last_stable(tmp_path: Path) -> None:
    """上次激活没走完（缺 .stable）→ 启动时自动退回上一个 stable 世代。"""
    asyncio.run(_recovery_flow(tmp_path))


def test_execv_restart_resumes_pending_generation(tmp_path: Path) -> None:
    """同一 PID 的状态快照说明是 execv 自重启，应继续验证而不是回滚。"""
    asyncio.run(_execv_resume_flow(tmp_path))


async def _recovery_flow(tmp_path: Path) -> None:
    good = _desired(tmp_path, generation="gen-1")
    first_id = _create(tmp_path, good, stable=True)
    broken = _desired(tmp_path, generation="gen-2", mode="exit1")
    second_id = _create(tmp_path, broken, stable=False)
    assert _store(tmp_path).current().id == second_id  # type: ignore[union-attr]

    # 状态快照由另一个 PID 写下 → 真实崩溃，不是 execv 自重启
    state = StateStore(good.supervisor.state_dir)
    state.write_state(
        ActualState(
            generation="gen-1",
            supervisor_pid=os.getpid() + 1,
            started_at=0.0,
            updated_at=0.0,
            generation_id=second_id,
        )
    )

    supervisor = _supervisor(broken)
    try:
        await supervisor.start()
        store = _store(tmp_path)
        current = store.current()
        assert current is not None
        assert current.id == first_id
        assert store.load(second_id).failed is True
        await _wait_for(lambda: _activation_phase(state) == "stable")
        await _wait_for(lambda: _status(state) is ServiceStatus.READY)
    finally:
        await supervisor.stop()


async def _execv_resume_flow(tmp_path: Path) -> None:
    """execv 自重启（同一 PID）应继续验证新世代，而不是回滚。"""
    first = _desired(tmp_path, generation="gen-1")
    first_id = _create(tmp_path, first, stable=True)
    second = _desired(tmp_path, generation="gen-2")
    second_id = _create(tmp_path, second, stable=False)

    # 旧 supervisor 在 execv 前写入的状态：PID 仍是当前进程
    state = StateStore(second.supervisor.state_dir)
    state.write_state(
        ActualState(
            generation="gen-1",
            supervisor_pid=os.getpid(),
            started_at=0.0,
            updated_at=0.0,
            generation_id=second_id,
            activation=ActivationState(phase="stable", generation_id=first_id),
        )
    )

    supervisor = _supervisor(second)
    try:
        await supervisor.start()
        await _wait_for(lambda: _activation_phase(state) == "stable")
        store = _store(tmp_path)
        current = store.current()
        assert current is not None
        assert current.id == second_id
        assert store.load(second_id).failed is False
        assert store.load(second_id).stable is True
    finally:
        await supervisor.stop()


def test_reload_survives_uncompilable_config(tmp_path: Path) -> None:
    """blue.infd 改坏了不该影响正在运行的世代（这是世代机制的核心承诺）。"""
    asyncio.run(_broken_config_flow(tmp_path))


async def _broken_config_flow(tmp_path: Path) -> None:
    desired = _desired(tmp_path, generation="gen-1")
    _create(tmp_path, desired, stable=True)
    desired.config_path.write_text("这不是合法配置 {{{\n", encoding="utf-8")
    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    try:
        await supervisor.start()
        assert await supervisor.reload() is True
        await _wait_for(lambda: _config_error(state) is not None)
        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.config_error is not None
        assert "编译失败" in snapshot.config_error
        await _wait_for(lambda: _status(state) is ServiceStatus.READY)
    finally:
        await supervisor.stop()


def test_activation_records_generation_ids_in_state(tmp_path: Path) -> None:
    """state.json 要能看出"跑的哪个世代、上一个稳定世代是哪个"。"""
    asyncio.run(_state_ids_flow(tmp_path))


async def _state_ids_flow(tmp_path: Path) -> None:
    first = _desired(tmp_path, generation="gen-1")
    first_id = _create(tmp_path, first, stable=True)
    supervisor = _supervisor(first)
    state = StateStore(first.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _status(state) is ServiceStatus.READY)

        second = _desired(tmp_path, generation="gen-2", mode="sleep")
        second_id = _create(tmp_path, second, stable=False)
        assert await supervisor.reload() is True
        await _wait_for(
            lambda: (
                _activation_phase(state) == "stable"
                and _generation_id(state) == second_id
            )
        )

        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.generation_id == second_id
        assert snapshot.last_stable_id == second_id
        # 服务进程应记录自己来自哪个产物（这里是自定义命令，故为空）
        assert snapshot.services["demo"].artifact_hash == ""
        assert first_id != second_id
    finally:
        await supervisor.stop()


@pytest.mark.parametrize("mode", ["exit1"])
def test_unready_service_keeps_generation_pending(tmp_path: Path, mode: str) -> None:
    """只要还有一个服务没就绪，世代就不能算 stable。"""
    asyncio.run(_pending_flow(tmp_path, mode))


async def _pending_flow(tmp_path: Path, mode: str) -> None:
    desired = _desired(tmp_path, generation="gen-1", mode=mode, activation_timeout=30.0)
    _create(tmp_path, desired, stable=False)
    supervisor = _supervisor(desired)
    state = StateStore(desired.supervisor.state_dir)
    try:
        await supervisor.start()
        await _wait_for(lambda: _activation_phase(state) == "pending")
        await asyncio.sleep(0.5)
        snapshot = state.read_state()
        assert snapshot is not None
        assert snapshot.activation is not None
        assert snapshot.activation.phase == "pending"
        assert snapshot.activation.failures == ["demo"]
    finally:
        await supervisor.stop()
