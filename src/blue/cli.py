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
from blue.control.logs import LogManager
from blue.control.model import ActualState, ServiceStatus
from blue.control.reconciler import Supervisor
from blue.control.runner import ProcessRunner
from blue.control.state import StateStore
from blue.errors import BlueError

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回进程退出码。"""
    args = _parser().parse_args(argv)
    _configure_logging(bool(args.verbose))
    config_path = cast(str, args.config)
    try:
        match args.command:
            case "up":
                return _up(config_path)
            case "apply":
                return _apply(
                    config_path,
                    wait=bool(args.wait),
                    timeout=float(args.timeout),
                )
            case "status":
                return _status(config_path, as_json=bool(args.json))
            case "logs":
                return _logs(
                    config_path,
                    name=cast(str, args.name),
                    tail=int(args.tail),
                    follow=bool(args.follow),
                )
            case "down":
                return _down(config_path, timeout=float(args.timeout))
            case "config-check":
                return _config_check(config_path, write=bool(args.write))
            case "render":
                return _render(config_path, full=bool(args.full))
            case "build":
                return _build(config_path)
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


def _up(config_path: str) -> int:
    """前台运行 supervisor，直到收到 SIGINT / SIGTERM。"""
    desired = load_desired(config_path)
    asyncio.run(_run_supervisor(desired))
    return 0


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
    )

    stop = asyncio.Event()
    reload_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    with suppress(NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, reload_requested.set)

    await supervisor.start()
    try:
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            if reload_requested.is_set():
                reload_requested.clear()
                supervisor.request_reload()
    finally:
        await supervisor.stop()


def _apply(config_path: str, *, wait: bool, timeout: float) -> int:
    """通知运行中的 supervisor 重新加载配置。"""
    desired = load_desired(config_path)
    state = StateStore(desired.supervisor.state_dir)
    running, info = state.supervisor_running()
    if not running or info is None:
        raise BlueError("supervisor 未运行；请先执行 blue up")
    try:
        os.kill(info.pid, signal.SIGHUP)
    except ProcessLookupError as error:
        raise BlueError(f"supervisor 进程不存在：{info.pid}") from error
    print(f"已通知 supervisor 重新加载（pid={info.pid}）")

    if wait:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = state.read_state()
            if current is not None:
                if current.config_error:
                    raise BlueError(f"配置重载失败：{current.config_error}")
                if current.generation == desired.generation:
                    print("配置已生效")
                    return 0
            time.sleep(0.1)
        raise BlueError(f"等待配置生效超时（{timeout}s）")
    return 0


def _status(config_path: str, *, as_json: bool) -> int:
    """打印 supervisor、服务与契约状态。"""
    desired = _try_load(config_path)
    state_dir = desired.supervisor.state_dir if desired is not None else Path(".blue")
    state = StateStore(state_dir)
    actual = state.read_state()
    running, info = state.supervisor_running()

    service_rows = _service_status_rows(desired, actual)
    contract_rows = _contract_status_rows(desired, actual)
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
            "state": actual.model_dump(mode="json") if actual is not None else None,
            "desired_generation": desired.generation if desired is not None else None,
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
        version = service.package_version or "-"
        print(
            f"  - {name:<18} kind={meta.kind:<6} restart={meta.restart:<10} "
            f"deps={deps:<18} contracts={contracts:<14} version={version:<10} "
            f"config={service.config_hash[:12]}"
        )
    print(f"contracts  : {len(desired.contracts)}")
    for name in sorted(desired.contracts):
        contract = desired.contracts[name]
        print(
            f"  - {name:<18} package={contract.meta.package:<24} "
            f"version={contract.package_version or '-':<10} "
            f"source={contract.source_hash[:12]}"
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

    subparsers.add_parser("up", help="前台运行 supervisor")

    apply_parser = subparsers.add_parser("apply", help="重新加载配置")
    apply_parser.add_argument(
        "--wait", action="store_true", help="等待新 generation 生效"
    )
    apply_parser.add_argument("--timeout", type=float, default=20.0, help="等待秒数")

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
    subparsers.add_parser("doctor", help="检查运行依赖")
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
