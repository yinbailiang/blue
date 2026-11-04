"""调谐器：让实际进程状态持续收敛到 blue.infd 的期望状态。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from blue.control.backoff import Backoff, should_restart
from blue.control.desired import (
    load_desired,
    redact_config,
    remove_runtime_config,
    topological_order,
    write_runtime_configs,
)
from blue.control.health import readiness_target, wait_ready
from blue.control.logs import LogManager
from blue.control.model import (
    ActualContract,
    ActualService,
    ActualState,
    DesiredContract,
    DesiredService,
    DesiredState,
    ServiceStatus,
)
from blue.control.plan import restart_reasons
from blue.control.process import signal_tree
from blue.control.runner import ProcessHandle, ProcessRunner
from blue.control.state import StateStore
from blue.errors import BlueError, ConfigError

logger = logging.getLogger(__name__)

__all__ = ["Supervisor", "build_service_env"]


def _changed_contracts(
    previous: dict[str, DesiredContract], current: dict[str, DesiredContract]
) -> set[str]:
    """返回新增、移除或内容/元数据发生变化的契约名。"""
    changed = set(previous) ^ set(current)
    for name in set(previous) & set(current):
        old = previous[name]
        new = current[name]
        if (
            old.meta != new.meta
            or old.package_version != new.package_version
            or old.source_hash != new.source_hash
        ):
            changed.add(name)
    return changed


def build_service_env(
    desired: DesiredState, service: DesiredService, *, log_dir: Path
) -> dict[str, str]:
    """组装服务进程的环境变量。

    包含总线连接信息、状态/日志目录、项目根（``blue.infd`` 所在目录）
    与当前服务的元数据。
    """
    settings = desired.supervisor
    return {
        "BLUE_PROJECT_ROOT": str(desired.base_dir),
        "BLUE_BUS_URL": desired.bus_url,
        "BLUE_BUS_TRANSPORT": desired.bus_transport,
        "BLUE_REGISTRY_TTL": str(settings.registry_ttl),
        "BLUE_REGISTRY_SWEEP_INTERVAL": str(settings.registry_sweep_interval),
        "BLUE_STATE_DIR": str(settings.state_dir),
        "BLUE_LOG_DIR": str(log_dir),
        "BLUE_SERVICE_KIND": service.meta.kind,
    }


@dataclass
class _Runtime:
    desired: DesiredService
    status: ServiceStatus = ServiceStatus.PENDING
    handle: ProcessHandle | None = None
    ready_task: asyncio.Task[None] | None = None
    instance_id: str | None = None
    restarts: int = 0
    consecutive_failures: int = 0
    next_start_at: float = 0.0
    last_exit_code: int | None = None
    last_error: str | None = None
    started_at: float | None = None
    ready_at: float | None = None
    restart_pending: bool = True
    stop_requested: bool = False


class Supervisor:
    """单机调谐器：启动、监控、重启、停止与配置热重载。"""

    def __init__(
        self,
        *,
        config_path: Path,
        desired: DesiredState,
        state: StateStore,
        logs: LogManager,
        runner: ProcessRunner,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_path = config_path.resolve()
        self._desired = desired
        self._state = state
        self._logs = logs
        self._runner = runner
        self._clock = clock
        self._backoff = Backoff(
            base=desired.supervisor.restart_backoff,
            maximum=desired.supervisor.restart_backoff_max,
        )
        self._runtimes: dict[str, _Runtime] = {
            name: _Runtime(desired=service)
            for name, service in desired.services.items()
        }
        self._started_at = time.time()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._reload_requested = False
        self._config_error: str | None = None
        self._dirty = True
        self._stopped = False

    @property
    def desired(self) -> DesiredState:
        """当前期望状态。"""
        return self._desired

    @property
    def config_error(self) -> str | None:
        """最近一次配置重载错误。"""
        return self._config_error

    async def start(self) -> None:
        """获取单实例锁、清理孤儿进程、写入配置并启动调谐循环。"""
        previous = self._state.read_state()
        self._state.acquire(generation=self._desired.generation)
        try:
            cleaned = await self._state.cleanup_orphans(previous)
            if cleaned:
                logger.info("已清理上一次遗留的服务进程：%s", ", ".join(cleaned))
            if previous is not None:
                for name in previous.services:
                    if name not in self._desired.services:
                        stale = (
                            self._desired.base_dir
                            / "services"
                            / name
                            / "service_runtime_config.json"
                        )
                        stale.unlink(missing_ok=True)
            changed = write_runtime_configs(self._desired)
            if changed:
                logger.info("已生成运行时配置：%s", ", ".join(changed))
            self._write_state()
            self._loop_task = asyncio.create_task(self._loop(), name="blue.supervisor")
        except Exception:
            self._state.release()
            raise
        logger.info(
            "supervisor 已启动（pid=%s，generation=%s）",
            os.getpid(),
            self._desired.generation,
        )

    async def stop(self) -> None:
        """请求调谐循环退出，并等待全部服务停止。"""
        if self._stopped:
            return
        self._stop_event.set()
        self._wake.set()
        loop_task = self._loop_task
        try:
            if loop_task is not None:
                await loop_task
        finally:
            self._state.release()
            self._stopped = True
        logger.info("supervisor 已停止")

    def request_reload(self) -> None:
        """请求在下一轮调谐中重新加载配置。"""
        self._reload_requested = True
        self._wake.set()

    async def reload(self) -> bool:
        """立即重新加载配置（测试与内部调用）。"""
        async with self._lock:
            return await self._reload_locked()

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                async with self._lock:
                    try:
                        await self._tick()
                    except Exception:
                        logger.exception("调谐轮次失败")
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self._desired.supervisor.reconcile_interval,
                    )
                self._wake.clear()
        finally:
            try:
                await self._shutdown()
            except Exception:
                logger.exception("supervisor 停机失败")

    async def _tick(self) -> None:
        now = self._clock()
        if self._reload_requested:
            self._reload_requested = False
            await self._reload_locked()

        for runtime in list(self._runtimes.values()):
            handle = runtime.handle
            if handle is not None and handle.process.returncode is not None:
                await self._on_exit(runtime)

        for name in topological_order(self._desired.services):
            runtime = self._runtimes.get(name)
            if runtime is None:
                continue
            if runtime.handle is not None or runtime.stop_requested:
                continue
            if not runtime.restart_pending:
                continue
            if now < runtime.next_start_at:
                continue
            if not self._dependencies_ready(runtime.desired):
                continue
            await self._start(runtime, now)

        stable_after = self._desired.supervisor.stable_after
        for runtime in self._runtimes.values():
            if (
                runtime.status is ServiceStatus.READY
                and runtime.ready_at is not None
                and now - runtime.ready_at >= stable_after
                and runtime.consecutive_failures
            ):
                runtime.consecutive_failures = 0
                self._dirty = True

        if self._dirty:
            self._write_state()
            self._dirty = False

    async def _reload_locked(self) -> bool:
        try:
            desired = load_desired(self._config_path)
        except (ConfigError, BlueError) as error:
            self._config_error = str(error)
            self._dirty = True
            logger.error("配置重载失败，保留上一份有效配置：%s", error)
            return False

        if desired.supervisor.state_dir != self._desired.supervisor.state_dir:
            self._config_error = "state_dir 变更需要重启 supervisor"
            self._dirty = True
            logger.error("配置重载失败：%s", self._config_error)
            return False

        changed_contracts = _changed_contracts(
            self._desired.contracts, desired.contracts
        )
        if changed_contracts:
            affected = sorted(
                name
                for name, service in desired.services.items()
                if set(service.meta.depends_contracts) & changed_contracts
            )
            logger.info(
                "契约已变化：%s；将重启：%s",
                ", ".join(sorted(changed_contracts)),
                ", ".join(affected) or "无",
            )

        try:
            write_runtime_configs(desired)
        except OSError as error:
            self._config_error = f"写入运行时配置失败：{error}"
            self._dirty = True
            logger.error("配置重载失败：%s", self._config_error)
            return False

        for name, service in desired.services.items():
            runtime = self._runtimes.get(name)
            if runtime is None:
                self._runtimes[name] = _Runtime(desired=service)
                continue
            reasons = restart_reasons(
                current=service,
                previous_meta=runtime.desired.meta,
                previous_config_hash=runtime.desired.config_hash,
                previous_source_hash=runtime.desired.source_hash,
                previous_version=runtime.desired.package_version,
                changed_contracts=changed_contracts,
            )
            if reasons:
                logger.info("将重启 %s：%s", name, "、".join(reasons))
                runtime.desired = service
                runtime.restart_pending = True
                runtime.next_start_at = 0.0
                if runtime.handle is not None:
                    await self._stop_runtime(runtime, ServiceStatus.STOPPING)
                runtime.status = ServiceStatus.PENDING
                runtime.last_error = None

        for name in list(self._runtimes):
            if name in desired.services:
                continue
            runtime = self._runtimes[name]
            runtime.stop_requested = True
            if runtime.handle is not None:
                await self._stop_runtime(runtime, ServiceStatus.STOPPING)
            remove_runtime_config(runtime.desired)
            del self._runtimes[name]

        self._desired = desired
        self._backoff = Backoff(
            base=desired.supervisor.restart_backoff,
            maximum=desired.supervisor.restart_backoff_max,
        )
        self._config_error = None
        self._dirty = True
        self._wake.set()
        logger.info("配置已重载（generation=%s）", desired.generation)
        return True

    async def _start(self, runtime: _Runtime, now: float) -> None:
        service = runtime.desired
        instance_id = f"{service.meta.name}-{uuid.uuid4().hex[:12]}"
        try:
            handle = await self._runner.start(
                service,
                instance_id=instance_id,
                env=self._service_env(service),
            )
        except Exception as error:
            runtime.last_error = str(error)
            runtime.status = ServiceStatus.BACKOFF
            runtime.consecutive_failures += 1
            runtime.next_start_at = now + self._backoff.delay(
                runtime.consecutive_failures
            )
            runtime.restart_pending = True
            self._dirty = True
            logger.error("启动服务 %s 失败：%s", service.meta.name, error)
            return

        runtime.handle = handle
        runtime.instance_id = instance_id
        runtime.status = ServiceStatus.STARTING
        runtime.started_at = time.time()
        runtime.ready_at = None
        runtime.last_exit_code = None
        runtime.last_error = None
        runtime.restart_pending = True
        runtime.ready_task = asyncio.create_task(
            self._wait_ready(runtime, handle),
            name=f"ready.{service.meta.name}",
        )
        self._dirty = True
        logger.info("服务已启动：%s（pid=%s）", service.meta.name, handle.pid)

    async def _wait_ready(self, runtime: _Runtime, handle: ProcessHandle) -> None:
        service = runtime.desired
        target = readiness_target(service.meta, service.config)
        try:
            ready = await wait_ready(
                handle.process,
                service.meta,
                timeout=service.meta.startup_timeout,
                target=target,
            )
        except Exception as error:
            logger.warning("服务 %s 就绪探针失败：%s", service.meta.name, error)
            ready = False
        if runtime.handle is not handle or runtime.stop_requested:
            return
        if ready and handle.process.returncode is None:
            runtime.status = ServiceStatus.READY
            runtime.ready_at = time.time()
            runtime.last_error = None
            self._dirty = True
            logger.info("服务已就绪：%s", service.meta.name)
            return

        runtime.ready_task = None
        runtime.last_error = (
            f"启动未在 {service.meta.startup_timeout}s 内就绪"
            if ready is False
            else "进程在就绪前退出"
        )
        await self._stop_runtime(runtime, ServiceStatus.STOPPING)
        self._schedule_restart(runtime, self._clock(), runtime.last_error)

    async def _on_exit(self, runtime: _Runtime) -> None:
        handle = runtime.handle
        if handle is None:
            return
        exit_code = await self._runner.wait(handle)
        if os.name == "posix":
            signal_tree(handle.process.pid, signal.SIGTERM)
        runtime.handle = None
        runtime.instance_id = None
        runtime.ready_at = None
        runtime.last_exit_code = exit_code
        self._cancel_ready(runtime)

        if runtime.stop_requested:
            runtime.status = ServiceStatus.STOPPED
            runtime.restart_pending = False
        elif should_restart(runtime.desired.meta.restart, exit_code):
            self._schedule_restart(
                runtime, self._clock(), f"进程退出（exit={exit_code}）"
            )
        else:
            runtime.status = (
                ServiceStatus.STOPPED if exit_code == 0 else ServiceStatus.FAILED
            )
            runtime.restart_pending = False
        self._dirty = True
        logger.info(
            "服务退出：%s（exit=%s，status=%s）",
            runtime.desired.meta.name,
            exit_code,
            runtime.status,
        )

    def _schedule_restart(self, runtime: _Runtime, now: float, message: str) -> None:
        runtime.consecutive_failures += 1
        runtime.restarts += 1
        runtime.last_error = message
        runtime.restart_pending = True
        runtime.next_start_at = now + self._backoff.delay(runtime.consecutive_failures)
        if (
            runtime.consecutive_failures
            >= self._desired.supervisor.restart_max_attempts
        ):
            runtime.status = ServiceStatus.CRASH_LOOP
        else:
            runtime.status = ServiceStatus.BACKOFF
        self._dirty = True
        logger.warning(
            "服务 %s 将在 %.2fs 后重启：%s",
            runtime.desired.meta.name,
            runtime.next_start_at - now,
            message,
        )

    async def _stop_runtime(
        self,
        runtime: _Runtime,
        status: ServiceStatus,
        *,
        timeout: float | None = None,
    ) -> None:
        task = self._cancel_ready(runtime)
        if task is not None and task is not asyncio.current_task():
            with suppress(asyncio.CancelledError):
                await task
        handle = runtime.handle
        runtime.handle = None
        runtime.instance_id = None
        if handle is not None:
            stop_timeout = (
                timeout if timeout is not None else runtime.desired.meta.stop_timeout
            )
            await self._runner.stop(handle, timeout=stop_timeout)
        runtime.status = status
        runtime.ready_at = None
        self._dirty = True

    def _cancel_ready(self, runtime: _Runtime) -> asyncio.Task[None] | None:
        task = runtime.ready_task
        runtime.ready_task = None
        if task is None or task is asyncio.current_task():
            return None
        if not task.done():
            task.cancel()
        return task

    def _dependencies_ready(self, service: DesiredService) -> bool:
        for name in service.meta.depends_on:
            runtime = self._runtimes.get(name)
            if runtime is None or runtime.status is not ServiceStatus.READY:
                return False
        return True

    def _service_env(self, service: DesiredService) -> dict[str, str]:
        return build_service_env(self._desired, service, log_dir=self._logs.log_dir)

    async def _shutdown(self) -> None:
        services = {name: runtime.desired for name, runtime in self._runtimes.items()}
        try:
            order = topological_order(services)
        except ConfigError:
            order = list(services)
        deadline = time.monotonic() + self._desired.supervisor.shutdown_timeout
        for name in reversed(order):
            runtime = self._runtimes.get(name)
            if runtime is None:
                continue
            runtime.stop_requested = True
            remaining = max(0.1, deadline - time.monotonic())
            timeout = min(runtime.desired.meta.stop_timeout, remaining)
            await self._stop_runtime(runtime, ServiceStatus.STOPPED, timeout=timeout)
        for runtime in self._runtimes.values():
            runtime.restart_pending = False
        self._write_state()
        logger.info("全部服务已停止")

    def _write_state(self) -> None:
        services: dict[str, ActualService] = {}
        for name, runtime in self._runtimes.items():
            handle = runtime.handle
            services[name] = ActualService(
                name=name,
                kind=runtime.desired.meta.kind,
                status=runtime.status,
                pid=handle.pid if handle is not None else None,
                pgid=handle.pgid if handle is not None else None,
                instance_id=runtime.instance_id,
                restarts=runtime.restarts,
                consecutive_failures=runtime.consecutive_failures,
                last_exit_code=runtime.last_exit_code,
                last_error=runtime.last_error,
                started_at=runtime.started_at,
                ready_at=runtime.ready_at,
                config_hash=runtime.desired.config_hash,
                config=redact_config(runtime.desired.config),
                package_version=runtime.desired.package_version,
                source_hash=runtime.desired.source_hash,
                log_path=self._logs.path_for(name),
            )
        state = ActualState(
            generation=self._desired.generation,
            supervisor_pid=os.getpid(),
            started_at=self._started_at,
            updated_at=time.time(),
            config_error=self._config_error,
            services=services,
            contracts={
                name: ActualContract(
                    name=name,
                    package=contract.meta.package,
                    module=contract.meta.module,
                    description=contract.meta.description,
                    package_version=contract.package_version,
                    source_hash=contract.source_hash,
                )
                for name, contract in self._desired.contracts.items()
            },
        )
        try:
            self._state.write_state(state)
        except OSError:
            logger.warning("写入 supervisor 状态失败", exc_info=True)
