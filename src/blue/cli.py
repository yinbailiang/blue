"""``blue`` 控制面命令行：调谐、状态、日志与配置校验。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import cast

from blue.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    DesiredState,
    load_desired,
    render_state,
    write_runtime_configs,
)
from blue.control import mcp
from blue.control import plan as planner
from blue.control.artifacts import ArtifactStore
from blue.control.desired import topological_order
from blue.control.generations import Generation, GenerationStore
from blue.control.logs import LogManager
from blue.control.model import ActualState, ServiceStatus
from blue.control.reconciler import Supervisor
from blue.control.runner import ProcessRunner
from blue.control.state import LockInfo, StateStore
from blue.errors import BlueError
from blue.runtime import RUNTIME_CONFIG_VERSION

logger = logging.getLogger(__name__)

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回进程退出码。"""
    args = _parser().parse_args(argv)
    _configure_logging(bool(args.verbose))
    config_path = cast(str, args.config)
    try:
        match args.command:
            case "up":
                return _up(config_path, foreground=bool(args.foreground))
            case "apply":
                return _apply(
                    config_path,
                    wait=bool(args.wait),
                    timeout=args.timeout,
                    staged=bool(args.staged),
                    build=not bool(args.no_build),
                )
            case "activate":
                return _activate(
                    config_path,
                    generation_id=int(args.generation),
                    wait=bool(args.wait),
                    timeout=args.timeout,
                )
            case "rollback":
                return _rollback(
                    config_path,
                    to=args.to,
                    service=args.service,
                    wait=bool(args.wait),
                    timeout=args.timeout,
                )
            case "generations":
                return _generations(config_path, as_json=bool(args.json))
            case "gc":
                return _gc(config_path, keep=args.keep, dry_run=bool(args.dry_run))
            case "status":
                return _status(config_path, as_json=bool(args.json))
            case "logs":
                return _logs(
                    config_path,
                    name=cast(str, args.name),
                    tail=int(args.tail),
                    follow=bool(args.follow),
                )
            case "_supervise":
                return _up(
                    cast(str, args.config_path) or config_path,
                    foreground=True,
                )
            case "down":
                return _down(config_path, timeout=float(args.timeout))
            case "config-check":
                return _config_check(config_path, write=bool(args.write))
            case "render":
                return _render(config_path, full=bool(args.full))
            case "build":
                return _build(config_path)
            case "plan":
                return _plan(
                    config_path, as_json=bool(args.json), show_diff=bool(args.diff)
                )
            case "doctor":
                return _doctor(config_path)
            case "mcp-tools":
                return _mcp_tools(
                    config_path,
                    only=cast(str, args.server),
                    as_json=bool(args.json),
                    timeout=float(args.timeout),
                )
            case _:
                print(f"未知命令：{args.command}", file=sys.stderr)
                return 2
    except BlueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _up(config_path: str, *, foreground: bool = False) -> int:
    """拉起调谐器进程。

    默认**后台常驻**：``up`` 直接返回，调谐器独立成进程（新会话），因此
    ``apply`` 可以让它 ``execv`` 重启自己来吃下 SDK 改动，不需要再 ``down``/``up``。
    ``--foreground`` 保留旧行为（容器/调试里有用）。
    """
    desired = load_desired(config_path)
    _ensure_initial_generation(desired)
    if foreground:
        asyncio.run(_run_supervisor(desired))
        return 0

    state = StateStore(desired.supervisor.state_dir)
    running, info = state.supervisor_running()
    if running and info is not None:
        print(f"supervisor 已在运行（pid={info.pid}）")
        return 0

    pid = _spawn_detached(config_path)
    print(f"supervisor 已启动（pid={pid}）")
    print(f"  日志：{state.log_dir}")
    print("  改完配置用 blue apply 生效；停止用 blue down")
    return 0


def _ensure_initial_generation(desired: DesiredState) -> None:
    """首次启动（或从旧版本升级）时补一个世代，让调谐器有 ``current`` 可依。"""
    store = _generation_store(desired)
    if store.current() is not None:
        return
    artifacts = _artifact_store(desired)
    _build_artifacts(desired, artifacts)
    generation = store.create(
        desired,
        artifacts,
        reason="bootstrap",
        created_by=_git_revision(desired.base_dir),
    )
    store.mark_stable(generation)
    store.activate(generation)
    print(f"已初始化世代 {generation.id:04d}（bootstrap）")


def _spawn_detached(config_path: str) -> int:
    """以新会话拉起 ``blue _supervise``，把 PID 返回给调用方。

    双 fork + ``setsid``：调谐器不再是终端的子进程，关掉终端也不会被 SIGHUP 带走；
    它的 stdout/stderr 落到 ``.blue/logs/supervisor.log``。
    """
    desired = load_desired(config_path)
    state = StateStore(desired.supervisor.state_dir)
    state.ensure_dirs()
    log_path = state.log_dir / "supervisor.log"
    command = [sys.executable, "-m", "blue", "_supervise", str(desired.config_path)]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(desired.base_dir),
            env=dict(os.environ),
        )
    return process.pid


async def _run_supervisor(desired: DesiredState) -> None:
    settings = desired.supervisor
    state = StateStore(settings.state_dir)
    logs = LogManager(
        state.log_dir,
        max_bytes=settings.log_max_bytes,
        backups=settings.log_backups,
    )
    runner = ProcessRunner(logs, uv_frozen=settings.uv_frozen)
    supervisor = Supervisor(
        config_path=desired.config_path,
        desired=desired,
        state=state,
        logs=logs,
        runner=runner,
        blue_version=_blue_version(),
    )

    stop = asyncio.Event()
    reload_requested = asyncio.Event()
    restart_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    with suppress(NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, reload_requested.set)
    with suppress(NotImplementedError, AttributeError):
        # SIGUSR1 = "你的代码变了，重启自己"：光重载配置换不掉正在跑的实现
        loop.add_signal_handler(signal.SIGUSR1, restart_requested.set)

    await supervisor.start()
    reexec = False
    try:
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            if restart_requested.is_set():
                restart_requested.clear()
                if await _maybe_self_restart(desired, supervisor):
                    reexec = True
                    break
            if reload_requested.is_set():
                reload_requested.clear()
                supervisor.request_reload()
    finally:
        await supervisor.stop()
    if reexec:
        _exec_self(desired)


async def _maybe_self_restart(desired: DesiredState, supervisor: Supervisor) -> bool:
    """收到 SIGUSR1 就照做——**判断权在发信号的一方**（新代码的 CLI）。

    这里曾经自己再算一遍 SDK 指纹来"确认"，那是**循环论证**：进程只能用自己的
    （旧的）代码算指纹。旧的 `_load_sdk` 有 bug（只覆盖 pyproject.toml）时，
    它算出的指纹永远等于自己启动时的那个，于是永远得出"代码没变"——真事故。

    唯一要守的是：配置能编译。execv 进一个坏配置会让新镜像直接退出，而此时没有
    外部看护进程，等于把系统弄挂；这种情况退化成重载，保留当前进程。
    """
    try:
        load_desired(desired.config_path)
    except BlueError as error:
        logger.error("配置编译失败，放弃自重启、改为重载：%s", error)
        supervisor.request_reload()
        return False
    logger.info("收到自重启指令：停全部服务 → execv 自己（新代码会重新拉起）")
    return True


def _exec_self(desired: DesiredState) -> None:
    """用新代码替换当前进程：PID 不变、锁不丢，服务随后按新代码全部拉起。"""
    argv = [sys.executable, "-m", "blue", "_supervise", str(desired.config_path)]
    logger.info("execv 重启调谐器：%s", " ".join(argv))
    with suppress(OSError):
        sys.stdout.flush()
        sys.stderr.flush()
    os.execv(sys.executable, argv)


def _artifact_store(desired: DesiredState) -> ArtifactStore:
    """按 ``supervisor`` 配置构造产物仓库。"""
    settings = desired.supervisor
    return ArtifactStore(
        settings.state_dir,
        project_root=desired.base_dir,
        python=settings.artifact_python,
        link_mode=settings.artifact_link_mode,
        warn_cross_device=settings.artifact_warn_cross_device,
    )


def _generation_store(desired: DesiredState) -> GenerationStore:
    """按 ``supervisor`` 配置构造世代仓库。"""
    return GenerationStore(desired.supervisor.state_dir)


def _build_artifacts(desired: DesiredState, artifacts: ArtifactStore) -> None:
    """按依赖顺序构建所有服务的产物（已存在的直接复用）。

    这一步**不碰运行中的系统**：失败时系统一个字节都没变。
    """
    ordered = topological_order(desired.services)
    total = len(ordered)
    for index, name in enumerate(ordered, start=1):
        service = desired.services[name]
        if service.meta.command:
            print(f"[{index}/{total}] {name:<14} 自定义命令，无产物")
            continue
        digest = artifacts.hash_for(service, desired)
        ref = artifacts.resolve(name, digest)
        if ref.complete:
            print(f"[{index}/{total}] {name:<14} 复用产物 {digest[:12]}")
            continue
        print(f"[{index}/{total}] {name:<14} 构建产物 {digest[:12]} …", flush=True)
        artifacts.ensure(service, desired)
    artifacts.ensure_base(desired)


def _git_revision(base_dir: Path) -> str:
    """记录世代对应的 git 提交（脏工作区加标记），best-effort 永不失败。"""

    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=base_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    revision = run("rev-parse", "--short", "HEAD")
    if not revision:
        return ""
    return f"git {revision}{' (dirty)' if run('status', '--porcelain') else ''}"


def _prepare_generation(
    config_path: str, *, reason: str, build: bool
) -> tuple[DesiredState, Generation]:
    """编译 → 构建产物 → 冻结世代目录（**不激活**）。"""
    desired = load_desired(config_path)
    artifacts = _artifact_store(desired)
    if build:
        _build_artifacts(desired, artifacts)
    generation = _generation_store(desired).create(
        desired,
        artifacts,
        reason=reason,
        created_by=_git_revision(desired.base_dir),
    )
    return desired, generation


def _notify_supervisor(desired: DesiredState, state: StateStore) -> None:
    """让调谐器按 ``current`` 重新加载（必要时先 execv 换掉自己的代码）。"""
    running, info = state.supervisor_running()
    if not running or info is None:
        raise BlueError("supervisor 未运行；请先执行 blue up")
    restart = _needs_self_restart(info, desired=desired)
    try:
        os.kill(info.pid, signal.SIGUSR1 if restart else signal.SIGHUP)
    except ProcessLookupError as error:
        raise BlueError(f"supervisor 进程不存在：{info.pid}") from error
    if restart:
        print(f"blue 代码已变化：已通知 supervisor 重启自己（pid={info.pid}）")
    else:
        print(f"已通知 supervisor 重新加载（pid={info.pid}）")


def _wait_for_activation(
    desired: DesiredState, generation: Generation, timeout: float | None
) -> int:
    """等该世代通过健康闸门；失败/回滚返回非 0。"""
    state = StateStore(desired.supervisor.state_dir)
    limit = (
        timeout if timeout is not None else desired.supervisor.activation_timeout + 30.0
    )
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        current = state.read_state()
        if current is not None:
            activation = current.activation
            if activation is not None and activation.generation_id == generation.id:
                if activation.phase == "stable":
                    _warn_if_stale_reconciler(state, desired)
                    print(f"世代 {generation.id:04d} 已稳定：全部服务就绪")
                    return 0
                if activation.phase in {"rolled_back", "failed"}:
                    message = activation.message or activation.phase
                    print(
                        f"❌ 世代 {generation.id:04d} 激活失败：{message}",
                        file=sys.stderr,
                    )
                    if activation.phase == "rolled_back":
                        print(
                            f"   已自动回滚到世代 {activation.previous_id}",
                            file=sys.stderr,
                        )
                    return 1
            if current.config_error:
                print(f"⚠️ {current.config_error}", file=sys.stderr)
        time.sleep(0.2)
    raise BlueError(f"等待世代 {generation.id:04d} 验证超时（{limit:.0f}s）")


def _apply(
    config_path: str,
    *,
    wait: bool,
    timeout: float | None,
    staged: bool = False,
    build: bool = True,
) -> int:
    """三阶段 apply：构建 → 原子切换 → 健康验证。

    ``--staged`` 只到"世代目录已就绪"为止（等价 NixOS 的 ``nixos-rebuild boot``），
    用 ``blue activate <N>`` 才真正切换。
    """
    desired, generation = _prepare_generation(config_path, reason="apply", build=build)
    store = _generation_store(desired)
    state = StateStore(desired.supervisor.state_dir)
    current = store.current()

    if staged:
        print(
            f"已生成世代 {generation.id:04d}（未激活）："
            f"执行 blue activate {generation.id} 生效"
        )
        return 0

    if current is not None and current.id == generation.id:
        print(
            f"配置未变化：沿用世代 {generation.id:04d}（{generation.generation[:12]}）"
        )
        _notify_supervisor(desired, state)
        return _wait_for_activation(desired, generation, timeout) if wait else 0

    store.activate(generation)
    parent = generation.parent_id
    print(
        f"已切换 current → 世代 {generation.id:04d}"
        f"（父世代 {parent if parent is not None else '无'}）"
    )
    _notify_supervisor(desired, state)
    return _wait_for_activation(desired, generation, timeout) if wait else 0


def _activate(
    config_path: str, *, generation_id: int, wait: bool, timeout: float | None
) -> int:
    """激活一个已存在的世代（配合 ``apply --staged``）。"""
    desired = load_desired(config_path)
    store = _generation_store(desired)
    generation = store.load(generation_id)
    store.activate(generation)
    print(f"已切换 current → 世代 {generation.id:04d}（{generation.generation[:12]}）")
    _notify_supervisor(desired, StateStore(desired.supervisor.state_dir))
    return _wait_for_activation(desired, generation, timeout) if wait else 0


def _rollback(
    config_path: str,
    *,
    to: int | None,
    service: str | None,
    wait: bool,
    timeout: float | None,
) -> int:
    """回滚：整机回到上一个（或指定）stable 世代，或只换一个服务的产物与配置。"""
    desired = load_desired(config_path)
    store = _generation_store(desired)
    current = store.current()
    if current is None:
        raise BlueError("没有当前世代（还没 apply 过），无从回滚")

    if service is not None:
        source = (
            store.load(to)
            if to is not None
            else _previous_service_generation(store, current, service)
        )
        target = store.rebind_service(
            service=service,
            source=source,
            created_by=_git_revision(desired.base_dir),
        )
        print(
            f"已构造世代 {target.id:04d}："
            f"{service} 回到世代 {source.id:04d} 的产物与配置"
        )
    else:
        resolved = (
            store.load(to)
            if to is not None
            else store.latest_stable(exclude=current.id)
        )
        if resolved is None:
            raise BlueError("没有更早的稳定世代可以回滚")
        target = resolved
        print(f"回滚：世代 {current.id:04d} → 世代 {target.id:04d}")

    store.activate(target)
    _notify_supervisor(desired, StateStore(desired.supervisor.state_dir))
    return _wait_for_activation(desired, target, timeout) if wait else 0


def _previous_service_generation(
    store: GenerationStore, current: Generation, service: str
) -> Generation:
    """找最近一个"该服务的产物或配置不同"的世代（单服务回滚的默认目标）。

    :raises BlueError: 没有任何这样的世代
    """
    if service not in current.services:
        raise BlueError(f"当前世代里没有服务 {service!r}")
    target = current.services[service]
    fingerprint = (target.artifact_hash, target.config_hash)
    for generation in reversed(store.list()):
        if generation.id == current.id or service not in generation.services:
            continue
        entry = generation.services[service]
        if (entry.artifact_hash, entry.config_hash) != fingerprint:
            return generation
    raise BlueError(f"没有更早的世代包含不同的 {service} 产物或配置，无法回滚")


def _generations(config_path: str, *, as_json: bool) -> int:
    """列出全部世代与当前指针。"""
    desired = _try_load(config_path)
    state_dir = desired.supervisor.state_dir if desired is not None else Path(".blue")
    store = GenerationStore(state_dir)
    generations = store.list()
    current = store.current()
    entries = [
        (generation, current is not None and generation.id == current.id)
        for generation in generations
    ]
    if as_json:
        payload: dict[str, object] = {
            "current": current.id if current is not None else None,
            "generations": [
                {
                    "id": generation.id,
                    "current": is_current,
                    "state": generation.state,
                    "generation": generation.generation,
                    "parent_id": generation.parent_id,
                    "created_at": generation.data.created_at,
                    "created_by": generation.data.created_by,
                    "reason": generation.data.reason,
                    "services": len(generation.services),
                }
                for generation, is_current in entries
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("尚无世代（还没 apply 过）")
        return 0
    print(f"{'ID':<6}{'STATE':<10}{'SERVICES':>9}  {'CREATED':<20}GENERATION  NOTE")
    for generation, is_current in entries:
        marker = "*" if is_current else " "
        created = _format_time(generation.data.created_at)
        note = generation.data.reason
        if generation.data.created_by:
            note = (
                f"{note} · {generation.data.created_by}"
                if note
                else generation.data.created_by
            )
        print(
            f"{marker}{generation.id:04d}  {generation.state:<10}"
            f"{len(generation.services):>9}  {created:<20}"
            f"{generation.generation[:12]}  {note}"
        )
    return 0


def _gc(config_path: str, *, keep: int | None, dry_run: bool) -> int:
    """回收老世代与无人引用的产物（数据面永不回收）。"""
    desired = load_desired(config_path)
    settings = desired.supervisor
    keep_count = keep if keep is not None else settings.generation_keep
    store = _generation_store(desired)
    artifacts = _artifact_store(desired)

    # 仍有存活进程在用的世代：只重启变了服务 → 进程会跨世代共存，
    # 老进程的 BLUE_CONFIG_FILE 还指着它启动时那一代，删了就崩（实测踩过）。
    in_use = _generations_in_use(desired)
    keep_hashes = store.keep_hashes(keep=keep_count, in_use=in_use)
    # 上一轮 apply 的世代（回滚目标）也得留着，否则 gc 完就没有回滚目标了
    bases = {
        generation.data.deps_hash or "nolock"
        for generation in store.list()
        if generation.id in store.protected_ids(keep=keep_count, in_use=in_use)
    }
    removed_generations = store.gc(keep=keep_count, dry_run=dry_run, in_use=in_use)
    removed_artifacts = artifacts.gc(keep_hashes, dry_run=dry_run)
    removed_bases = artifacts.gc_bases(bases, dry_run=dry_run)

    verb = "将删除" if dry_run else "已删除"
    print(
        f"{verb}世代 {len(removed_generations)} 个："
        + (", ".join(f"{gid:04d}" for gid in removed_generations) or "无")
    )
    if in_use:
        print(
            "保留运行中进程仍在用的世代："
            + ", ".join(f"{gid:04d}" for gid in sorted(in_use))
        )
    print(
        f"{verb}产物 {len(removed_artifacts)} 个："
        + (", ".join(removed_artifacts[:6]) or "无")
        + (" …" if len(removed_artifacts) > 6 else "")
    )
    print(
        f"{verb}依赖基座 {len(removed_bases)} 个：" + (", ".join(removed_bases) or "无")
    )
    return 0


def _generations_in_use(desired: DesiredState) -> set[int]:
    """从状态快照里捞出"还有存活进程在用"的世代号。"""
    snapshot = StateStore(desired.supervisor.state_dir).read_state()
    if snapshot is None:
        return set()
    return {
        service.generation_id
        for service in snapshot.services.values()
        if service.generation_id
    }


def _current_generation_id(state_dir: Path) -> int | None:
    """``current`` 指向的世代号（没有则为 ``None``）。"""
    current = GenerationStore(state_dir).current()
    return current.id if current is not None else None


def _warn_if_stale_reconciler(store: StateStore, desired: DesiredState) -> None:
    """自重启信号发了但锁里的 SDK 指纹没更新 → 对方是旧代码，得手动重启一次。

    旧版调谐器不认识 SIGUSR1（或它自己判断"代码没变"），会静默按重载处理，
    结果是"apply 说成功了，但跑的还是旧代码"。必须显式告警，否则很难发现。
    """
    running, info = store.supervisor_running()
    if not running or info is None or not info.sdk_hash:
        return
    if info.sdk_hash == desired.sdk.source_hash:
        return
    print(
        "⚠️ 运行中的调谐器仍是旧代码（锁里的 SDK 指纹未更新）：\n"
        "   它不认识自重启信号，请执行一次 blue down && blue up 完成迁移。",
        file=sys.stderr,
    )


def _needs_self_restart(info: LockInfo, *, desired: DesiredState) -> bool:
    """运行中的调谐器是否需要**重启自己**（而不是热重载配置）。

    三种情况必须自重启：blue 代码/SDK 指纹变了、包版本变了、运行时配置格式变了。
    它们都换不掉一个**已经在跑的** Python 进程里的代码——只能 ``execv``。
    只改服务配置/服务源码时返回 ``False``（SIGHUP 即可）。
    """
    if info.config_version != RUNTIME_CONFIG_VERSION:
        return True
    current_version = _blue_version()
    if (
        info.blue_version
        and current_version != "unknown"
        and info.blue_version != current_version
    ):
        return True
    sdk_now = desired.sdk.source_hash
    return bool(info.sdk_hash) and bool(sdk_now) and info.sdk_hash != sdk_now


def _status(config_path: str, *, as_json: bool) -> int:
    """打印 supervisor、服务与契约状态。"""
    desired = _try_load(config_path)
    state_dir = desired.supervisor.state_dir if desired is not None else Path(".blue")
    state = StateStore(state_dir)
    actual = state.read_state()
    running, info = state.supervisor_running()

    service_rows = _service_status_rows(desired, actual)
    contract_rows = _contract_status_rows(desired, actual)
    sdk_rows = _sdk_status_rows(desired, actual)
    blue_version = _blue_version()

    if as_json:
        payload = {
            "supervisor": {
                "running": running,
                "pid": info.pid if info is not None else None,
            },
            "blue_version": blue_version,
            "contracts": {
                name: {
                    "name": name,
                    "package": package,
                    "version": version,
                    "source_hash": source_hash,
                    "used_by": [] if used_by == "-" else used_by.split(","),
                }
                for name, package, version, source_hash, used_by in contract_rows
            },
            "sdk": {
                name: {
                    "name": name,
                    "package": package,
                    "version": version,
                    "source_hash": source_hash,
                    "used_by": [] if used_by == "-" else used_by.split(","),
                }
                for name, package, version, source_hash, used_by in sdk_rows
            },
            "state": actual.model_dump(mode="json") if actual is not None else None,
            "desired_generation": desired.generation if desired is not None else None,
            "current_generation": _current_generation_id(state_dir),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if running and info is not None:
        print(f"supervisor: running (pid={info.pid})")
    else:
        print("supervisor: stopped")
    print(f"blue       : {blue_version}")

    if actual is None and desired is None:
        print("尚无状态快照（supervisor 可能未运行）")
        return 0
    if actual is not None:
        if actual.config_error:
            print(f"配置错误：{actual.config_error}")
        print(
            f"generation : {actual.generation[:12]}  "
            f"updated={_format_time(actual.updated_at)}"
        )
        current_generation = GenerationStore(state_dir).current()
        if current_generation is not None:
            print(
                f"世代       : {current_generation.id:04d} "
                f"{current_generation.state:<8}"
                f"{current_generation.generation[:12]}  "
                f"{current_generation.data.created_by}"
            )
        activation = actual.activation
        if activation is not None:
            detail = activation.message
            if activation.failures:
                detail = "未就绪：" + ", ".join(activation.failures[:5])
            suffix = f"（{detail}）" if detail else ""
            print(f"激活       : {activation.phase}{suffix}")
        if desired is not None and desired.generation != actual.generation:
            print(
                f"desired    : {desired.generation[:12]}  "
                "（配置已变化，执行 blue apply）"
            )
        if not actual.services and desired is not None:
            print("尚无运行快照；显示期望状态")
    elif desired is not None:
        print(f"generation : {desired.generation[:12]}  （期望状态）")

    print(f"services   : {len(service_rows)}")
    print(
        f"{'NAME':<18}{'KIND':<8}{'VERSION':<10}{'STATUS':<18}"
        f"{'PID':>8}{'RESTARTS':>10}  MESSAGE"
    )
    for name, kind, version, status, pid, restarts, message in service_rows:
        print(
            f"{name:<18}{kind:<8}{version:<10}{status:<18}"
            f"{pid:>8}{restarts:>10}  {message}"
        )

    print(f"contracts  : {len(contract_rows)}")
    if contract_rows:
        print(f"{'NAME':<18}{'PACKAGE':<26}{'VERSION':<10}{'SOURCE':<14}USED BY")
        for name, package, version, source_hash, used_by in contract_rows:
            print(
                f"{name:<18}{package:<26}{version or '-':<10}"
                f"{source_hash[:12]:<14}{used_by}"
            )

    print(f"sdk        : {len(sdk_rows)}")
    if sdk_rows:
        print(f"{'NAME':<18}{'PACKAGE':<26}{'VERSION':<10}{'SOURCE':<14}USED BY")
        for name, package, version, source_hash, used_by in sdk_rows:
            print(
                f"{name:<18}{package:<26}{version or '-':<10}"
                f"{source_hash[:12]:<14}{used_by}"
            )
    return 0


def _blue_version() -> str:
    """控制面包版本；开发环境下读已安装分布。"""
    try:
        return dist_version("blue")
    except PackageNotFoundError:
        return "unknown"


def _service_status_rows(
    desired: DesiredState | None, actual: ActualState | None
) -> list[tuple[str, str, str, str, str, str, str]]:
    """服务展示行：实际快照优先，缺失时回退到期望状态。"""
    if actual is not None and actual.services:
        rows: list[tuple[str, str, str, str, str, str, str]] = []
        for service in actual.services.values():
            version = service.package_version
            if not version and desired is not None:
                desired_service = desired.services.get(service.name)
                version = desired_service.package_version if desired_service else ""
            rows.append(
                (
                    service.name,
                    service.kind,
                    version or "-",
                    service.status.value,
                    str(service.pid) if service.pid is not None else "-",
                    str(service.restarts),
                    service.last_error or "",
                )
            )
        return sorted(rows)
    if desired is None:
        return []
    return sorted(
        (
            name,
            service.meta.kind,
            service.package_version or "-",
            ServiceStatus.STOPPED.value,
            "-",
            "0",
            "",
        )
        for name, service in desired.services.items()
    )


def _contract_status_rows(
    desired: DesiredState | None, actual: ActualState | None
) -> list[tuple[str, str, str, str, str]]:
    """契约展示行：name/package/version/source/used_by。"""
    usage: dict[str, list[str]] = {}
    if desired is not None:
        for service_name, service in desired.services.items():
            for contract in service.meta.depends_contracts:
                usage.setdefault(contract, []).append(service_name)

    rows: list[tuple[str, str, str, str, str]] = []
    if actual is not None and actual.contracts:
        for contract in actual.contracts.values():
            used_by = ",".join(sorted(usage.get(contract.name, []))) or "-"
            rows.append(
                (
                    contract.name,
                    contract.package,
                    contract.package_version,
                    contract.source_hash,
                    used_by,
                )
            )
    elif desired is not None:
        for contract in desired.contracts.values():
            used_by = ",".join(sorted(usage.get(contract.meta.name, []))) or "-"
            rows.append(
                (
                    contract.meta.name,
                    contract.meta.package,
                    contract.package_version,
                    contract.source_hash,
                    used_by,
                )
            )
    return sorted(rows)


def _sdk_status_rows(
    desired: DesiredState | None, actual: ActualState | None
) -> list[tuple[str, str, str, str, str]]:
    """SDK 运行时包展示行：name/package/version/source/used_by。"""
    usage: dict[str, list[str]] = {}
    if desired is not None:
        for service_name, service in desired.services.items():
            for dependency in service.meta.depends_sdk:
                usage.setdefault(dependency, []).append(service_name)

    rows: list[tuple[str, str, str, str, str]] = []
    packages = actual.sdk.packages if actual is not None and actual.sdk else None
    if packages:
        for name, package in packages.items():
            used_by = ",".join(sorted(usage.get(name, []))) or "-"
            rows.append(
                (
                    name,
                    package.meta.package,
                    package.package_version,
                    package.source_hash,
                    used_by,
                )
            )
    elif desired is not None:
        for name, package in desired.sdk.packages.items():
            used_by = ",".join(sorted(usage.get(name, []))) or "-"
            rows.append(
                (
                    name,
                    package.meta.package,
                    package.package_version,
                    package.source_hash,
                    used_by,
                )
            )
    return sorted(rows)


def _logs(config_path: str, *, name: str, tail: int, follow: bool) -> int:
    """查看服务日志。"""
    desired = _try_load(config_path)
    state_dir = desired.supervisor.state_dir if desired is not None else Path(".blue")
    logs = LogManager(StateStore(state_dir).log_dir)
    if follow:
        asyncio.run(_follow_logs(logs, name, tail))
        return 0
    lines = logs.tail(name, tail)
    if not lines:
        print(f"日志不存在或为空：{logs.path_for(name)}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


async def _follow_logs(logs: LogManager, name: str, tail: int) -> None:
    for line in logs.tail(name, tail):
        print(line)
    async for line in logs.follow(name):
        print(line, flush=True)


def _down(config_path: str, *, timeout: float) -> int:
    """优雅停止 supervisor；超时后强杀。"""
    desired = _try_load(config_path)
    state_dir = desired.supervisor.state_dir if desired is not None else Path(".blue")
    state = StateStore(state_dir)
    running, info = state.supervisor_running()
    if not running or info is None:
        print("supervisor 未运行")
        return 0
    try:
        os.kill(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        print("supervisor 进程已不存在")
        return 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not StateStore.pid_alive(info.pid):
            print("supervisor 已停止")
            return 0
        time.sleep(0.1)
    with suppress(ProcessLookupError):
        os.kill(info.pid, signal.SIGKILL)
    print(f"supervisor 未在 {timeout}s 内退出，已强制结束")
    return 1


def _config_check(config_path: str, *, write: bool) -> int:
    """编译并打印期望状态摘要；可选生成运行时配置。"""
    desired = load_desired(config_path)
    settings = desired.supervisor
    print(f"配置有效：{desired.config_path}")
    print(f"generation : {desired.generation}")
    print(f"state_dir  : {settings.state_dir}")
    print(f"bus        : {desired.bus_transport} @ {desired.bus_url}")
    print(f"services   : {len(desired.services)}")
    for name in sorted(desired.services):
        service = desired.services[name]
        meta = service.meta
        deps = ",".join(meta.depends_on) or "-"
        contracts = ",".join(meta.depends_contracts) or "-"
        sdk = ",".join(meta.depends_sdk) or "-"
        version = service.package_version or "-"
        print(
            f"  - {name:<18} kind={meta.kind:<6} restart={meta.restart:<10} "
            f"deps={deps:<18} contracts={contracts:<14} sdk={sdk:<10} "
            f"version={version:<10} config={service.config_hash[:12]}"
        )
    print(f"contracts  : {len(desired.contracts)}")
    for name in sorted(desired.contracts):
        contract = desired.contracts[name]
        print(
            f"  - {name:<18} package={contract.meta.package:<24} "
            f"version={contract.package_version or '-':<10} "
            f"source={contract.source_hash[:12]}"
        )
    print(f"sdk        : {len(desired.sdk.packages)}")
    for name in sorted(desired.sdk.packages):
        package = desired.sdk.packages[name]
        print(
            f"  - {name:<18} package={package.meta.package:<24} "
            f"version={package.package_version or '-':<10} "
            f"source={package.source_hash[:12]}"
        )
    if write:
        changed = write_runtime_configs(desired, only_changed=False)
        print(f"已写入运行时配置：{', '.join(changed) if changed else '无'}")
    return 0


def _render(config_path: str, *, full: bool) -> int:
    """打印期望状态（默认脱敏）。"""
    desired = load_desired(config_path)
    print(
        json.dumps(
            render_state(desired, redact=not full),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build(config_path: str) -> int:
    """同步 uv workspace 的全部服务包。"""
    desired = load_desired(config_path)
    uv = shutil.which("uv")
    if uv is None:
        raise BlueError("未找到 uv")
    completed = subprocess.run(
        [uv, "sync", "--all-packages"],
        cwd=desired.base_dir,
        check=False,
    )
    return completed.returncode


def _plan(config_path: str, *, as_json: bool, show_diff: bool = False) -> int:
    """预览一次 apply 会做什么，不接触 supervisor、不改任何文件。

    退出码：``0`` 无变化、``1`` 有变化、``2`` 配置无法编译——方便脚本用
    ``if blue plan -q; then …`` 判断"要不要 apply"。
    """
    desired = _try_load(config_path)
    if desired is None:
        return 2
    state = StateStore(desired.supervisor.state_dir)
    actual = state.read_state()
    result = planner.plan(desired, actual)

    if as_json:
        print(
            json.dumps(
                {
                    "desired_generation": result.desired_generation,
                    "actual_generation": result.actual_generation,
                    "has_changes": result.has_changes,
                    "counts": result.counts(),
                    "services": [
                        {
                            "name": change.name,
                            "action": change.action,
                            "reasons": list(change.reasons),
                            "status": change.status,
                            "config_diff": list(change.config_diff),
                        }
                        for change in result.services
                    ],
                    "contracts": [
                        {
                            "name": change.name,
                            "action": change.action,
                            "before": change.before,
                            "after": change.after,
                            "affected": list(change.affected),
                        }
                        for change in result.contracts
                    ],
                    "sdk": [
                        {
                            "name": change.name,
                            "action": change.action,
                            "before": change.before,
                            "after": change.after,
                            "affected": list(change.affected),
                        }
                        for change in result.sdk
                    ],
                    "note": result.note,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if result.has_changes else 0

    current = result.actual_generation or "（未部署）"
    print(f"generation：当前 {current[:12]} → 期望 {result.desired_generation[:12]}")

    interesting = [c for c in result.services if c.changed]
    unchanged = [c for c in result.services if not c.changed]
    if interesting:
        print("\n服务变更")
        for change in interesting:
            marker = {"create": "+", "restart": "~", "remove": "-"}[change.action]
            reason = "；".join(change.reasons) or ""
            detail = f"  {reason}" if reason else ""
            print(f"  {marker} {change.name:<16}{detail}")
            if show_diff:
                if change.config_diff:
                    for line in change.config_diff:
                        print(f"      {line}")
                elif change.action == "restart" and "配置变化" in change.reasons:
                    print("      （旧快照里没有配置内容，apply 一次后即可对比字段）")
    if result.contracts:
        print("\n契约变更")
        for change in result.contracts:
            before = change.before.split(":")[1][:8] if ":" in change.before else "-"
            after = change.after.split(":")[1][:8] if ":" in change.after else "-"
            affected = ", ".join(change.affected) or "无"
            print(f"  ~ {change.name:<16}{before} → {after}   影响：{affected}")
    if result.sdk:
        print("\nSDK 变更")
        for change in result.sdk:
            before = change.before[:8] or "-"
            after = change.after[:8] or "-"
            affected = ", ".join(change.affected) or "无"
            marker = {"create": "+", "remove": "-", "change": "~"}[change.action]
            print(f"  {marker} {change.name:<16}{before} → {after}   影响：{affected}")
    if unchanged and interesting:
        names = ", ".join(change.name for change in unchanged)
        print(f"\n不变（{len(unchanged)}）：{names}")

    tally = result.counts()
    print(
        f"\n汇总：新增 {tally['create']} · 重启 {tally['restart']} · "
        f"删除 {tally['remove']} · 不变 {tally['keep']}"
    )
    if interesting:
        restarted = ", ".join(result.restarted) or "无"
        print(f"重启期间这些服务会短暂不可用：{restarted}")
    if result.note:
        print(f"\n{result.note}")
    return 1 if result.has_changes else 0


def _mcp_tools(config_path: str, *, only: str, as_json: bool, timeout: float) -> int:
    """列出 MCP server 真正提供的工具名，并标出白名单覆盖情况。

    用途：``mcp_bridge.tools`` 白名单必须写对工具名，而不同版本的 server 会改名
    （kwin-mcp 0.12 是 ``list_windows``，1.0 改成 ``window_list``）。这里直接把
    server 的 ``tools/list`` 打出来，照着抄即可。
    """
    desired = _try_load(config_path)
    if desired is None:
        return 1
    bridge = desired.services.get("mcp_bridge")
    if bridge is None:
        print("配置里没有启用 mcp_bridge 服务")
        return 1
    root = Path(config_path).resolve().parent
    declared = mcp.declared_tool_names(bridge.config)
    servers = [
        server
        for server in mcp.declared_servers(bridge.config)
        if server.enabled and (not only or server.name == only)
    ]
    if only and not servers:
        print(f"没有名为 {only!r} 的已启用 server", file=sys.stderr)
        return 1

    report: dict[str, dict[str, object]] = {}
    ok = True
    for server in servers:
        problem = mcp.command_problem(server.command, cwd=root)
        tools: list[mcp.ToolSummary] = []
        error = problem
        if error is None:
            try:
                tools = asyncio.run(mcp.probe_tools(server, cwd=root, timeout=timeout))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        covered = sum(1 for tool in tools if f"{server.name}__{tool.name}" in declared)
        report[server.name] = {
            "command": server.display,
            "tools": [
                {
                    "name": tool.name,
                    "declared": f"{server.name}__{tool.name}" in declared,
                    "description": tool.description,
                }
                for tool in tools
            ],
            "declared": covered,
            "error": error,
        }
        if error is not None:
            ok = False

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    for server in servers:
        entry = report[server.name]
        print(f"\n{server.name}  ({server.display})")
        error = entry["error"]
        if error is not None:
            print(f"  ✗ {error}")
            continue
        entries = cast(list[dict[str, object]], entry["tools"])
        print(f"  提供 {len(entries)} 个工具，白名单已启用 {entry['declared']} 个")
        for item in entries:
            mark = "✓" if item["declared"] else "·"
            print(f"  {mark} {item['name']}")
    if not ok:
        print("\n有 server 探测失败，见上面的错误", file=sys.stderr)
    return 0 if ok else 1


def _doctor(config_path: str) -> int:
    """检查关键运行时依赖。"""
    desired = _try_load(config_path)
    ok = True
    uv = shutil.which("uv")
    print(f"uv            : {uv or '未找到'}")
    ok = ok and uv is not None

    rabbit = shutil.which("rabbitmq-server")
    print(f"rabbitmq-server: {rabbit or '未找到'}")
    if desired is not None:
        bus = desired.services.get("bus")
        managed = bool(
            bus is not None
            and bus.config.get("mode", "managed") == "managed"
            and bus.config.get("transport", "rabbit") == "rabbit"
        )
        if managed and rabbit is None:
            print("  -> bus 为 managed 模式，但未找到 rabbitmq-server")
            ok = False
        state_dir = desired.supervisor.state_dir
    else:
        state_dir = Path(".blue")
    print(f"state_dir     : {state_dir}")
    if desired is not None:
        bridge = desired.services.get("mcp_bridge")
        if bridge is not None:
            root = Path(config_path).resolve().parent
            servers = mcp.declared_servers(bridge.config)
            for server in servers:
                if not server.enabled:
                    print(f"mcp server {server.name:<12}: 已禁用")
                    continue
                problem = mcp.command_problem(server.command, cwd=root)
                if problem is None:
                    print(f"mcp server {server.name:<12}: ok（{server.display}）")
                else:
                    print(f"mcp server {server.name:<12}: {problem}")
                    ok = False
            declared = mcp.declared_tool_names(bridge.config)
            for entry in mcp.unknown_tool_servers(
                declared, {server.name for server in servers}
            ):
                print(f"mcp 白名单     : {entry} 指向未声明的 server")
                ok = False
    return 0 if ok else 1


def _try_load(config_path: str) -> DesiredState | None:
    try:
        return load_desired(config_path)
    except ConfigError as error:
        print(f"配置无效：{error}", file=sys.stderr)
        return None


def _configure_logging(verbose: bool) -> None:
    """配置统一日志格式；verbose 打开 DEBUG。"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _format_time(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="blue", description="分布式 Agent 系统控制面")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="核心配置文件路径"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出 DEBUG 日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser("up", help="后台拉起调谐器（默认）")
    up_parser.add_argument(
        "--foreground",
        action="store_true",
        help="前台运行（容器/调试用；Ctrl-C 即停）",
    )

    apply_parser = subparsers.add_parser("apply", help="编译 → 构建产物 → 原子切换世代")
    apply_parser.add_argument(
        "--wait", action="store_true", help="等待新世代通过健康验证"
    )
    apply_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="等待验证的秒数（默认 activation_timeout + 30）",
    )
    apply_parser.add_argument(
        "--staged",
        action="store_true",
        help="只生成世代、不激活（之后用 blue activate <N>；等价 nixos-rebuild boot）",
    )
    apply_parser.add_argument(
        "--no-build", action="store_true", help="不构建产物，只切换世代"
    )

    activate_parser = subparsers.add_parser("activate", help="激活一个已存在的世代")
    activate_parser.add_argument("generation", type=int, help="世代号")
    activate_parser.add_argument("--wait", action="store_true", help="等待健康验证")
    activate_parser.add_argument("--timeout", type=float, default=None)

    rollback_parser = subparsers.add_parser(
        "rollback", help="回滚：整机或单个服务回到旧世代"
    )
    rollback_parser.add_argument("--to", type=int, default=None, help="目标世代号")
    rollback_parser.add_argument(
        "--service", default=None, help="只回滚这个服务（产物 + 配置一起换）"
    )
    rollback_parser.add_argument("--wait", action="store_true", help="等待健康验证")
    rollback_parser.add_argument("--timeout", type=float, default=None)

    generations_parser = subparsers.add_parser("generations", help="列出全部世代")
    generations_parser.add_argument("--json", action="store_true", help="输出 JSON")

    gc_parser = subparsers.add_parser("gc", help="回收老世代与无人引用的产物")
    gc_parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="保留多少个最近世代（默认 generation_keep）",
    )
    gc_parser.add_argument("--dry-run", action="store_true", help="只列出，不删除")

    status_parser = subparsers.add_parser("status", help="查看服务状态")
    status_parser.add_argument("--json", action="store_true", help="输出 JSON")

    logs_parser = subparsers.add_parser("logs", help="查看服务日志")
    logs_parser.add_argument("name", help="服务名")
    logs_parser.add_argument("--tail", type=int, default=100, help="末尾行数")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="持续跟踪")

    down_parser = subparsers.add_parser("down", help="停止 supervisor 与全部服务")
    down_parser.add_argument("--timeout", type=float, default=30.0, help="等待秒数")

    check_parser = subparsers.add_parser("config-check", help="校验配置")
    check_parser.add_argument("--write", action="store_true", help="同时生成运行时配置")

    render_parser = subparsers.add_parser("render", help="打印期望状态")
    render_parser.add_argument("--full", action="store_true", help="不脱敏（包含密钥）")

    subparsers.add_parser("build", help="uv sync --all-packages")
    plan_parser = subparsers.add_parser(
        "plan", help="预览一次 apply 会新增/重启/删除哪些服务（对标 kubectl diff）"
    )
    plan_parser.add_argument("--json", action="store_true", help="输出 JSON")
    plan_parser.add_argument("--diff", action="store_true", help="展示配置的字段级变化")
    subparsers.add_parser("doctor", help="检查运行依赖")

    # 内部命令：up 的后台调谐器与 execv 自重启都靠它，不出现在 --help 里。
    supervise_parser = subparsers.add_parser("_supervise", help=argparse.SUPPRESS)
    supervise_parser.add_argument("config_path", nargs="?", default=None)

    mcp_parser = subparsers.add_parser(
        "mcp-tools", help="列出 MCP server 真实提供的工具名（写白名单用）"
    )
    mcp_parser.add_argument(
        "server", nargs="?", default="", help="只探测某个 server（缺省为全部）"
    )
    mcp_parser.add_argument("--json", action="store_true", help="输出 JSON")
    mcp_parser.add_argument(
        "--timeout", type=float, default=30.0, help="握手与 tools/list 超时（秒）"
    )
    return parser
