"""调谐器：让实际进程状态持续收敛到 blue.infd 的期望状态。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from blue.control.artifacts import ArtifactStore
from blue.control.backoff import Backoff, should_restart
from blue.control.desired import (
    load_desired,
    redact_config,
    remove_runtime_config,
    topological_order,
    write_runtime_configs,
)
from blue.control.generations import Generation, GenerationStore
from blue.control.health import readiness_target, wait_ready
from blue.control.logs import LogManager
from blue.control.model import (
    ActivationState,
    ActualContract,
    ActualSdk,
    ActualService,
    ActualState,
    DesiredContract,
    DesiredService,
    DesiredState,
    ServiceStatus,
)
from blue.control.plan import changed_packages, restart_reasons
from blue.control.process import signal_tree
from blue.control.runner import ProcessHandle, ProcessRunner
from blue.control.state import StateStore
from blue.errors import BlueError, ConfigError
from blue.runtime import (
    DEFAULT_RUNTIME_CONFIG,
    RUNTIME_CONFIG_VERSION,
    clear_shutdown_intents,
    shutdown_intent_dir,
)

logger = logging.getLogger(__name__)

__all__ = ["Supervisor", "dependency_levels", "shutdown_waves", "shutdown_worst_case"]


def dependency_levels(dependencies: Mapping[str, Iterable[str]]) -> dict[str, int]:
    """每个服务的依赖层级：没有（在场的）依赖为 0，其余为「依赖的最大层级 + 1」。

    层级就是停机的并发边界：**同一层之间没有依赖关系**，可以同时停；跨层必须按层级
    从高到低串行——依赖别人的先停、被别人依赖的后停（bus 永远在最后一层，这样前面
    的服务停的时候还有总可用，能正常收尾 / ack）。

    ``dependencies`` 里出现环时按 0 兜底（配置校验已经会拒绝成环，这里只是不让
    收尾路径被一个坏配置拖住）。
    """
    levels: dict[str, int] = {}

    def compute(name: str, trail: frozenset[str]) -> int:
        known = levels.get(name)
        if known is not None:
            return known
        deps = [dep for dep in dependencies.get(name, ()) if dep in dependencies]
        if not deps or name in trail:
            levels[name] = 0
            return 0
        value = 1 + max(compute(dep, trail | {name}) for dep in deps)
        levels[name] = value
        return value

    for name in dependencies:
        compute(name, frozenset())
    return levels


def shutdown_waves(levels: Mapping[str, int]) -> list[list[str]]:
    """把服务按层级分成停机波次：层级高的先停，同一波内丢给 ``asyncio.gather``。

    波内排序只为让日志与测试稳定，不承载任何语义。
    """
    buckets: dict[int, list[str]] = {}
    for name, level in levels.items():
        buckets.setdefault(level, []).append(name)
    return [sorted(buckets[level]) for level in sorted(buckets, reverse=True)]


def shutdown_worst_case(
    levels: Mapping[str, int], stop_timeouts: Mapping[str, float]
) -> float:
    """最坏停机时长：波与波串行，每波要等这波里最慢的服务。

    并发把「Σ 各服务」压成了「Σ 每波最慢」——18 个服务的 30 秒不再是 540 秒，
    而是「波数 × 30 秒」。
    """
    total = 0.0
    for wave in shutdown_waves(levels):
        total += max((stop_timeouts.get(name, 0.0) for name in wave), default=0.0)
    return total


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
        blue_version: str = "",
    ) -> None:
        self._config_path = config_path.resolve()
        self._state = state
        self._logs = logs
        self._runner = runner
        self._clock = clock
        self._blue_version = blue_version
        settings = desired.supervisor
        self._generations = GenerationStore(settings.state_dir)
        self._artifacts = ArtifactStore(
            settings.state_dir,
            project_root=desired.base_dir,
            python=settings.artifact_python,
            link_mode=settings.artifact_link_mode,
            warn_cross_device=settings.artifact_warn_cross_device,
        )
        self._config_error: str | None = None
        self._activation: ActivationState | None = None
        self._activation_deadline = 0.0
        self._active_id: int | None = None
        # 世代优先于 blue.infd：只要 current 指向一个完好世代，就是它说了算
        active = self._generations.current()
        if active is not None:
            try:
                desired = active.to_desired(
                    supervisor=settings,
                    base_dir=desired.base_dir,
                    artifacts=self._artifacts,
                )
            except BlueError as error:
                # 回落到刚编译出来的配置：系统仍可用，但状态里必须留下原因
                self._config_error = (
                    f"世代 {active.id} 无法还原，已退回 blue.infd：{error}"
                )
                logger.error(self._config_error)
        self._desired = desired
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
        self._state.acquire(
            generation=self._desired.generation,
            blue_version=self._blue_version,
            sdk_hash=self._desired.sdk.source_hash,
            config_version=RUNTIME_CONFIG_VERSION,
        )
        try:
            cleaned = await self._state.cleanup_orphans(previous)
            if cleaned:
                logger.info("已清理上一次遗留的服务进程：%s", ", ".join(cleaned))
            stale = clear_shutdown_intents(self._desired.supervisor.state_dir)
            if stale:
                logger.warning(
                    "清理了上一次停机残留的下线意图：%s（那次 down 没停完，"
                    "留着会把普通重启误切成 offline 并丢掉积压）",
                    ", ".join(stale),
                )
            self._recover_generation(previous)
            if self._generations.current() is None:
                # 尚未世代化（首次启动或从旧版本升上来）：沿用工作区配置
                if previous is not None:
                    for name in previous.services:
                        if name not in self._desired.services:
                            service_dir = self._desired.base_dir / "services" / name
                            (service_dir / DEFAULT_RUNTIME_CONFIG).unlink(
                                missing_ok=True
                            )
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

    def request_offline_shutdown(self) -> bool:
        """按服务写「下线意图」文件，让它们在停机时切到 ``offline``。

        用文件而不是信号：``write_text`` 返回后才发 ``SIGTERM``，服务一定能读到；
        两个信号则要靠事件循环的处理顺序，早一轮晚一轮都会让切换失效。

        **每个服务一份文件**：服务读到后先删再切策略，共享一份时第一个停机的服务
        会把它删掉，后面的服务全按 ``restart`` 留着按队列——于是 ``blue down`` 之后
        队列里还留着本不该留的积压（实测复现过：三个服务只有第一个走 offline）。

        服务读到后把策略从 ``restart`` 切成 ``offline``（解绑 + 排空），于是
        ``blue down`` 之后不留积压；普通重启不写这些文件，保持 ``restart`` 的
        「保留积压、启动后补投」语义。

        返回是否至少写成功一份。写失败不要紧：那个服务会按 ``restart`` 处理——积压
        留下而不是丢消息，这个降级方向是安全的。
        """
        directory = shutdown_intent_dir(self._desired.supervisor.state_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("写下线意图失败，本次 down 按 restart 处理（积压保留）")
            return False
        payload = json.dumps({"mode": "offline", "ts": time.time()})
        written = 0
        for name in sorted(self._desired.services):
            try:
                (directory / f"{name}.json").write_text(payload, encoding="utf-8")
            except OSError:
                logger.warning(
                    "服务 %s 的下线意图写入失败，它将按 restart 处理（积压保留）", name
                )
                continue
            written += 1
        return written > 0

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

        await self._check_activation(now)

    def _recover_generation(self, previous: ActualState | None) -> None:
        """启动时判断上次激活有没有走完；没走完就退回上一个 stable 世代。

        这就是 NixOS "上次启动失败就选旧的 boot entry" 的等价物：不需要人值守，
        也不会停在一个既没验证通过、也没回滚的中间态上。

        但 **execv 自重启**（apply 改到控制面代码时）与崩溃长得一样：`current`
        指向的新世代还没稳定。区别在 PID：execv 保留 PID，而崩溃后由
        ``blue up`` / 进程管理器拉起的进程一定有新 PID。因此同一个 PID 写下的
        状态快照说明是自重启，应当继续验证而不是回滚。
        """
        current = self._generations.current()
        if current is None:
            return
        if current.stable:
            self._active_id = current.id
            self._activation = ActivationState(
                phase="stable",
                generation_id=current.id,
                started_at=self._started_at,
            )
            return
        fallback = self._generations.latest_stable(exclude=current.id)
        if self._is_execv_continuation(previous):
            logger.info(
                "检测到控制面 execv 自重启（pid=%s）：继续验证世代 %s，不回滚",
                os.getpid(),
                current.id,
            )
            self._active_id = current.id
            self._begin_activation(
                current, previous_id=fallback.id if fallback is not None else None
            )
            return
        if fallback is None:
            logger.warning(
                "世代 %s 未验证通过，且没有更早的稳定世代可退；继续验证它", current.id
            )
            self._active_id = current.id
            self._begin_activation(current, previous_id=None)
            return
        logger.warning(
            "上次激活世代 %s 未完成，自动回退到世代 %s", current.id, fallback.id
        )
        self._generations.mark_failed(current, "activation interrupted")
        self._generations.activate(fallback)
        try:
            self._desired = fallback.to_desired(
                supervisor=self._desired.supervisor,
                base_dir=self._desired.base_dir,
                artifacts=self._artifacts,
            )
        except BlueError as error:
            self._config_error = f"回退到世代 {fallback.id} 失败：{error}"
            logger.error(self._config_error)
            return
        self._runtimes = {
            name: _Runtime(desired=service)
            for name, service in self._desired.services.items()
        }
        self._active_id = fallback.id
        self._activation = ActivationState(
            phase="stable",
            generation_id=fallback.id,
            previous_id=current.id,
            started_at=self._started_at,
            message=f"世代 {current.id} 上次激活未完成，已回退",
        )

    def _is_execv_continuation(self, previous: ActualState | None) -> bool:
        """上一次状态是否由**同一个 PID** 写入：execv 保留 PID，崩溃重启不会。"""
        return previous is not None and previous.supervisor_pid == os.getpid()

    def _begin_activation(
        self, generation: Generation | None, *, previous_id: int | None
    ) -> None:
        """登记一次"等待验证"的激活（到期未全绿就按失败处理）。"""
        if generation is None:
            self._activation = None
            self._activation_deadline = 0.0
            return
        timeout = self._desired.supervisor.activation_timeout
        now = time.time()
        self._activation = ActivationState(
            phase="pending",
            generation_id=generation.id,
            previous_id=previous_id,
            started_at=now,
            deadline=now + timeout,
        )
        self._activation_deadline = self._clock() + timeout
        self._dirty = True
        logger.info(
            "开始验证世代 %s（上限 %.0fs，前一个世代 %s）",
            generation.id,
            timeout,
            previous_id,
        )

    async def _check_activation(self, now: float) -> None:
        """健康闸门：全部服务就绪才算激活成功，超时就回滚。"""
        activation = self._activation
        if activation is None or activation.phase != "pending":
            return
        generation = self._generations.current()
        if generation is None or generation.id != activation.generation_id:
            return
        pending = sorted(
            name
            for name, runtime in self._runtimes.items()
            if runtime.status is not ServiceStatus.READY
        )
        if not pending:
            self._generations.mark_stable(generation)
            activation.phase = "stable"
            activation.message = ""
            activation.failures = []
            self._activation_deadline = 0.0
            self._dirty = True
            logger.info(
                "世代 %s 已稳定：%s 个服务全部就绪", generation.id, len(self._runtimes)
            )
            return
        activation.failures = pending
        if now < self._activation_deadline:
            return
        self._dirty = True
        await self._fail_activation(
            generation,
            f"未在 {self._desired.supervisor.activation_timeout:.0f}s 内就绪："
            + "、".join(pending),
        )

    async def _fail_activation(self, generation: Generation, message: str) -> None:
        """激活失败：标记并（按配置）自动回滚到上一个 stable 世代。"""
        failures = list(self._activation.failures) if self._activation else []
        self._generations.mark_failed(generation, message)
        fallback = self._generations.latest_stable(exclude=generation.id)
        logger.error("世代 %s 激活失败：%s", generation.id, message)
        if not self._desired.supervisor.generation_auto_rollback or fallback is None:
            self._activation = ActivationState(
                phase="failed",
                generation_id=generation.id,
                started_at=time.time(),
                message=message,
                failures=failures,
            )
            self._activation_deadline = 0.0
            return
        logger.error("自动回滚：世代 %s → 世代 %s", generation.id, fallback.id)
        self._generations.activate(fallback)
        self._active_id = None  # 强制下一次重载重建运行时
        reloaded = await self._reload_locked()
        self._activation = ActivationState(
            phase="rolled_back",
            generation_id=generation.id,
            previous_id=fallback.id,
            started_at=time.time(),
            message=message if reloaded else f"{message}（回滚重载失败，见日志）",
            failures=failures,
        )
        self._activation_deadline = 0.0
        self._dirty = True

    async def _reload_locked(self) -> bool:
        """按 ``current`` 指向的世代重建期望状态；尚未世代化时编译 ``blue.infd``。

        ``blue.infd`` 编译失败**不**会打断正在运行的世代——那是世代机制的核心
        承诺：配置改坏了，系统照旧跑，只是下一次 apply 无法构建。
        """
        generation = self._generations.current()
        compiled: DesiredState | None = None
        try:
            compiled = load_desired(self._config_path)
        except (ConfigError, BlueError) as error:
            if generation is None:
                self._config_error = str(error)
                self._dirty = True
                logger.error("配置重载失败，保留上一份有效配置：%s", error)
                return False
            self._config_error = f"blue.infd 编译失败（运行中的世代不受影响）：{error}"
            logger.warning(self._config_error)

        if compiled is not None:
            settings = compiled.supervisor
        else:
            settings = self._desired.supervisor

        if generation is not None:
            try:
                desired = generation.to_desired(
                    supervisor=settings,
                    base_dir=self._desired.base_dir,
                    artifacts=self._artifacts,
                )
            except BlueError as error:
                self._config_error = str(error)
                self._dirty = True
                logger.error("世代 %s 无法还原：%s", generation.id, error)
                return False
        else:
            if compiled is None:
                return False
            desired = compiled
            try:
                write_runtime_configs(desired)
            except OSError as error:
                self._config_error = f"写入运行时配置失败：{error}"
                self._dirty = True
                logger.error("配置重载失败：%s", self._config_error)
                return False

        # 生效的控制面设置以期望状态为准（世代里冻结的那份优先），
        # 但 state_dir 是运行期常量，改了只能重启 supervisor。
        if desired.supervisor.state_dir != self._state.state_dir:
            self._config_error = (
                "state_dir 变更需要重启 supervisor："
                f"运行中 {self._state.state_dir}，期望 {desired.supervisor.state_dir}"
            )
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

        sdk_touched = (
            self._desired.sdk.package_version,
            self._desired.sdk.source_hash,
        ) != (desired.sdk.package_version, desired.sdk.source_hash)
        if sdk_touched:
            logger.info(
                "blue SDK 核心已变化，全部服务将重启（runtime/transport 在核心里）"
            )

        packages_touched = changed_packages(
            self._desired.sdk.packages, desired.sdk.packages
        )
        if packages_touched:
            package_affected = sorted(
                name
                for name, service in desired.services.items()
                if set(service.meta.depends_sdk) & packages_touched
            )
            logger.info(
                "SDK 运行时包已变化：%s；将重启：%s",
                ", ".join(sorted(packages_touched)),
                ", ".join(package_affected) or "无",
            )

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
                sdk_changed=sdk_touched,
                changed_packages=packages_touched,
                previous_artifact_hash=runtime.desired.artifact_hash,
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
            if generation is None:
                # 世代化之后配置文件属于历史世代，删掉会破坏回滚
                remove_runtime_config(runtime.desired)
            del self._runtimes[name]

        self._desired = desired
        self._backoff = Backoff(
            base=desired.supervisor.restart_backoff,
            maximum=desired.supervisor.restart_backoff_max,
        )
        if compiled is not None:
            self._config_error = None
        self._dirty = True
        self._wake.set()
        if generation is not None and (
            generation.id != self._active_id
            or (self._activation is not None and self._activation.phase != "stable")
        ):
            # 换了世代要验证；同世代但上次验证失败（failed/rolled_back）也重试一次
            self._begin_activation(generation, previous_id=self._active_id)
        self._active_id = generation.id if generation is not None else None
        logger.info("配置已重载（generation=%s）", desired.generation)
        return True

    async def _start(self, runtime: _Runtime, now: float) -> None:
        service = runtime.desired
        instance_id = f"{service.meta.name}-{uuid.uuid4().hex[:12]}"
        try:
            handle = await self._runner.start(service, instance_id=instance_id)
        except Exception as error:
            runtime.last_error = str(error)
            if service.meta.restart == "never":
                # 拉起就失败（例如 command 不存在）：restart=never 就不该反复重试
                runtime.status = ServiceStatus.FAILED
                runtime.restart_pending = False
                self._dirty = True
                logger.error("服务 %s 启动失败：%s", service.meta.name, error)
                return
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

    async def _shutdown(self) -> None:
        services = {name: runtime.desired for name, runtime in self._runtimes.items()}
        dependencies = {
            name: service.meta.depends_on for name, service in services.items()
        }
        levels = dependency_levels(dependencies)
        waves = shutdown_waves(levels)
        timeouts = {
            name: service.meta.stop_timeout for name, service in services.items()
        }
        configured = self._desired.supervisor.shutdown_timeout
        worst = shutdown_worst_case(levels, timeouts)
        first_wave = (
            max((timeouts.get(name, 0.0) for name in waves[0]), default=0.0)
            if waves
            else 0.0
        )
        if first_wave > configured:
            logger.warning(
                "shutdown_timeout=%.1fs 连第一波（%s）都装不下："
                "这波最慢的服务要 %.1fs，必然会 SIGKILL；建议提到 %.1fs 以上",
                configured,
                ", ".join(waves[0]),
                first_wave,
                first_wave,
            )
        else:
            logger.info(
                "停机开始：%d 个服务分 %d 波（同波并发、跨波串行），总预算 %.1fs，"
                "每波最多等最慢的那个服务，各层都跑满时的上界 %.1fs",
                len(services),
                len(waves),
                configured,
                worst,
            )
        deadline = time.monotonic() + configured
        for index, wave in enumerate(waves, start=1):
            runtimes = [self._runtimes[name] for name in wave if name in self._runtimes]
            if not runtimes:
                continue
            for runtime in runtimes:
                runtime.stop_requested = True
            remaining = max(0.1, deadline - time.monotonic())
            wave_budget = max(runtime.desired.meta.stop_timeout for runtime in runtimes)
            if remaining < wave_budget:
                # 只有真被压缩时才告警：上界（层数 × 30s）超过总预算本身是常态，
                # 每次都报反而没人看；这里指的是"钱真的花完了，这一波要打折停"。
                logger.warning(
                    "第 %d/%d 波只剩 %.1fs，本层最慢的服务要 %.1fs：被截断的服务会"
                    " SIGKILL，已投递未派发的消息可能被重投",
                    index,
                    len(waves),
                    remaining,
                    wave_budget,
                )
            results = await asyncio.gather(
                *(
                    self._stop_runtime(
                        runtime,
                        ServiceStatus.STOPPED,
                        timeout=min(runtime.desired.meta.stop_timeout, remaining),
                    )
                    for runtime in runtimes
                ),
                return_exceptions=True,
            )
            # 一个服务停不下来不能拖住后面的波次（尤其不能让 bus 永远等不到停止）
            for runtime, result in zip(runtimes, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        "停止服务 %s 失败：%s",
                        runtime.desired.meta.name,
                        result,
                        exc_info=result,
                    )
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
                artifact_hash=runtime.desired.artifact_hash,
                generation_id=runtime.desired.generation_id,
                log_path=self._logs.path_for(name),
            )
        current = self._generations.current()
        latest_stable = self._generations.latest_stable()
        state = ActualState(
            generation=self._desired.generation,
            supervisor_pid=os.getpid(),
            started_at=self._started_at,
            updated_at=time.time(),
            config_error=self._config_error,
            services=services,
            generation_id=current.id if current is not None else None,
            last_stable_id=latest_stable.id if latest_stable is not None else None,
            activation=self._activation,
            sdk=ActualSdk(
                package_version=self._desired.sdk.package_version,
                source_hash=self._desired.sdk.source_hash,
                packages=self._desired.sdk.packages,
            ),
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
