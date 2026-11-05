"""给 agent 自己用的重启助手：分离执行 + 等收敛 + 回一条消息。

**为什么需要它**：工具进程本身就是 blue 的服务。`blue apply`（控制面代码变了会
execv）或 `blue down/up` 会把这些服务一起重启，于是"发起重启的那条命令"被
SIGTERM 打断（工具调用以 `-15` 返回、输出丢失），接下来只能盲猜系统好了没有。

这个脚本把顺序倒过来：

1. `fork + setsid` 把真正的工作放进独立会话，父进程立刻返回；
   于是工具调用不会被打断，也拿得到"已派发"的确认。
2. 子进程执行命令 → 轮询 `.blue/state.json` 直到**收敛**（supervisor 活着 +
   全部服务 Ready + 激活 phase 稳定），或判定为终态失败（`failed` / `rolled_back`）。
3. 结果写进 `.blue/last-restart.json`，并通过 `session.outbox.enqueue` 往
   **当前会话**排一条消息——队列是持久化的，agent 下一轮开口就能看到
   "重启完成/失败"，不用盲等。

用法::

    uv run python scripts/blue_restart.py apply            # apply（不改内容则秒回）
    uv run python scripts/blue_restart.py down-up          # 彻底停再起
    uv run python scripts/blue_restart.py report           # 只读：上次结果
    uv run python scripts/blue_restart.py status           # 只读：当前收敛摘要

`--no-notify` 可以只用文件通知（总线不可用时自动降级成这个）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_NAME = "last-restart.json"
NOTIFY_ROLE = "agent.self-notify"

# 从 supervisor 继承来的变量会让 CLI/运行时误以为自己是某个服务实例：
# BLUE_CONFIG_FILE 可能指向已被 gc 回收的世代，BLUE_SERVICE_NAME 还会让
# 通知队列和真正的服务撞名。子进程里统一清掉。
_INHERITED_ENV = (
    "BLUE_SERVICE_NAME",
    "BLUE_INSTANCE_ID",
    "BLUE_CONFIG_FILE",
    "BLUE_ARTIFACT_HASH",
)

_COMMANDS: dict[str, list[str]] = {
    "apply": ["apply"],
    "down-up": ["down", "up"],
}


@dataclass
class Snapshot:
    """`.blue/state.json` 里跟"收敛"有关的那几个字段。"""

    alive: bool = False
    total: int = 0
    ready: int = 0
    not_ready: list[str] = field(default_factory=list[str])
    generation_id: int | None = None
    generation: str = ""
    phase: str | None = None
    failures: list[str] = field(default_factory=list[str])
    message: str = ""
    config_error: str | None = None
    updated_at: float = 0.0

    @property
    def converged(self) -> bool:
        """是否已收敛：supervisor 活着、全部服务 Ready、激活不是中间态。"""
        if not self.alive or self.total == 0 or self.ready != self.total:
            return False
        return self.phase in (None, "stable")

    @property
    def terminal_failure(self) -> str | None:
        """终态失败（不必再等）：返回原因，否则 None。

        只看**激活相位**。``config_error`` 不算终态失败：配置坏掉时系统通常仍在
        旧世代上正常跑（这正是世代机制要的效果），把它当失败会误报——命令的退出码
        已经能说明 apply 本身是否失败。
        """
        if self.phase in ("failed", "rolled_back"):
            detail = "、".join(self.failures) or self.message or "未知"
            return f"激活{self.phase}：{detail}"
        return None


def state_path(state_dir: Path) -> Path:
    return state_dir / "state.json"


def result_path(state_dir: Path) -> Path:
    return state_dir / RESULT_NAME


def pid_alive(pid: int) -> bool:
    """进程是否活着（用不上 /proc 的权限，signal 0 就够）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state(state_dir: Path) -> tuple[dict[str, Any] | None, Snapshot]:
    """读状态快照；读不到时返回全空的 :class:`Snapshot`。"""
    try:
        payload: object = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None, Snapshot()
    if not isinstance(payload, dict):
        return None, Snapshot()
    raw = cast("dict[str, Any]", payload)

    services_raw = raw.get("services")
    services: dict[str, Any] = (
        cast("dict[str, Any]", services_raw) if isinstance(services_raw, dict) else {}
    )
    not_ready = sorted(
        name
        for name, item in services.items()
        if isinstance(item, dict) and item.get("status") != "Ready"
    )
    activation = raw.get("activation")
    activation_raw: dict[str, Any] = (
        cast("dict[str, Any]", activation) if isinstance(activation, dict) else {}
    )
    failures_raw = activation_raw.get("failures")
    failures = (
        [str(item) for item in failures_raw] if isinstance(failures_raw, list) else []
    )
    pid = int(raw.get("supervisor_pid") or 0)
    generation_id = raw.get("generation_id")
    snapshot = Snapshot(
        alive=pid_alive(pid),
        total=len(services),
        ready=len(services) - len(not_ready),
        not_ready=not_ready,
        generation_id=int(generation_id) if isinstance(generation_id, int) else None,
        generation=str(raw.get("generation") or ""),
        phase=str(activation_raw.get("phase")) if activation_raw.get("phase") else None,
        failures=failures,
        message=str(activation_raw.get("message") or ""),
        config_error=str(raw["config_error"]) if raw.get("config_error") else None,
        updated_at=float(raw.get("updated_at") or 0.0),
    )
    return raw, snapshot


def wait_converged(
    state_dir: Path, *, timeout: float, after: float, poll: float = 0.5
) -> tuple[Snapshot, bool]:
    """轮询到收敛 / 终态失败 / 超时。

    :param after: 只接受 ``updated_at`` 不早于它的快照——避免读到重启前的旧状态
        （execv 后 PID 不变，光看 PID 分辨不出是不是新的）
    :return: ``(最后快照, 是否收敛)``
    """
    deadline = time.monotonic() + timeout
    snapshot = Snapshot()
    while time.monotonic() < deadline:
        _, snapshot = read_state(state_dir)
        fresh = snapshot.updated_at >= after
        if fresh and snapshot.converged:
            return snapshot, True
        if fresh and snapshot.terminal_failure is not None:
            return snapshot, False
        time.sleep(poll)
    return snapshot, False


def pick_session(state_dir: Path) -> str | None:
    """挑"最近活跃"的会话（就是当前这轮对话）。

    只读连接，绝不写会话库——会话库归 session 服务所有。
    """
    database = state_dir / "sessions.db"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def bus_url_for(state_dir: Path) -> str | None:
    """从当前世代的 agent 配置里取总线地址；取不到再看环境变量。"""
    current = state_dir / "current"
    config = current / "services" / "agent" / "service_runtime_config.json"
    try:
        payload: object = json.loads(config.read_text(encoding="utf-8"))
    except OSError, ValueError:
        payload = None
    if isinstance(payload, dict):
        infra = payload.get("infra")
        if isinstance(infra, dict):
            url = infra.get("bus_url")
            if isinstance(url, str) and url:
                return url
    return os.environ.get("BLUE_BUS_URL") or None


def render_message(result: dict[str, Any]) -> str:
    """把结果渲染成一条短消息（agent 下一轮直接读）。"""
    head = "🔄 重启完成" if result.get("ok") else "❌ 重启未收敛"
    parts = [
        f"{head}（{' → '.join(result.get('commands', ['?']))}）",
        f"世代 {_fmt_generation(result.get('generation_id'))}"
        f" {result.get('phase') or '-'}",
        f"{result.get('ready')}/{result.get('total')} Ready",
        f"耗时 {result.get('elapsed_s', 0):.0f}s",
    ]
    line = "，".join(parts)
    detail = result.get("detail")
    if detail:
        line += f"\n原因：{detail}"
    warning = result.get("config_error")
    if warning:
        line += f"\n提示：blue.infd 有错（旧世代仍在跑）：{warning}"
    not_ready = result.get("not_ready") or []
    if not_ready:
        line += f"\n未就绪：{'、'.join(str(item) for item in not_ready[:6])}"
    if result.get("timed_out"):
        line += "\n（已到等待上限，后续还可能自己好；可用 report 复查）"
    line += f"\n日志：{result.get('log')}"
    return line


def _fmt_generation(generation_id: object) -> str:
    if isinstance(generation_id, int):
        return f"{generation_id:04d}"
    return "?"


async def _enqueue(
    *, bus_url: str | None, session_id: str, text: str, timeout: float = 10.0
) -> bool:
    """通过 `session.outbox.enqueue` 往会话里排一条消息。"""
    from blue_contracts_session import (
        SessionOutboxEnqueueEvent,
        SessionOutboxEnqueueReplyEvent,
        SessionOutboxEnqueueResponse,
    )
    from infinity_bus.templates.request import request

    from blue.runtime import build_runtime

    previous_name = os.environ.get("BLUE_SERVICE_NAME")
    os.environ["BLUE_SERVICE_NAME"] = NOTIFY_ROLE
    runtime = build_runtime(
        NOTIFY_ROLE,
        events=[SessionOutboxEnqueueEvent, SessionOutboxEnqueueReplyEvent],
        url=bus_url,
    )
    try:
        await runtime.start()
        proxy = runtime.proxy(NOTIFY_ROLE)
        response = await request(
            proxy,
            SessionOutboxEnqueueEvent.name,
            {"text": text, "attachments": []},
            SessionOutboxEnqueueReplyEvent.name,
            session_id=session_id,
            timeout=timeout,
        )
        return isinstance(response, SessionOutboxEnqueueResponse) and response.success
    finally:
        await runtime.stop()
        if previous_name is None:
            os.environ.pop("BLUE_SERVICE_NAME", None)
        else:
            os.environ["BLUE_SERVICE_NAME"] = previous_name


def notify(result: dict[str, Any], *, state_dir: Path, session_id: str | None) -> bool:
    """给会话回一条消息；任何失败都只是"退回文件通知"，不影响结果。"""
    target = session_id or pick_session(state_dir)
    if target is None:
        return False
    try:
        return asyncio.run(
            _enqueue(
                bus_url=bus_url_for(state_dir),
                session_id=target,
                text=render_message(result),
            )
        )
    except Exception:  # 总线不通是常态（重启期间），不该让脚本失败
        return False


_FAILURE_MARKERS = ("冒烟失败", "激活失败", "配置编译失败", "错误：", "❌")
"""日志里表示"真的失败了"的行首标记；比盲取尾部靠谱得多。"""


def _failure_reason(log_path: Path) -> str:
    """从日志里挑出真正的失败原因。

    直接取尾部会把回滚之后的"已稳定"当成结论——实测踩过：冒烟失败的回执里
    最后几行是回滚重载的成功日志，读起来像是成功了。
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in _FAILURE_MARKERS):
            return stripped[:400]
    return _log_tail(log_path, 4)


def _log_tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def run_worker(
    *,
    commands: list[str],
    state_dir: Path,
    log_path: Path,
    wait: float,
    notify_wanted: bool,
    session_id: str | None,
) -> int:
    """子进程里真正干活：执行命令 → 等收敛 → 写结果 → 回消息。"""
    started_at = time.time()
    exit_code = 0
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== {time.strftime('%F %T')} {commands} ===\n")
        log.flush()
        for command in commands:
            # 命令来自本脚本的白名单，不含用户输入
            result = subprocess.run(
                ["uv", "run", "blue", command],
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if result.returncode != 0:
                exit_code = result.returncode
                break

    snapshot, converged = wait_converged(
        state_dir, timeout=wait, after=started_at - 1.0
    )
    failure = snapshot.terminal_failure
    ok = exit_code == 0 and converged
    detail = ""
    if exit_code != 0:
        detail = f"blue {commands[0]} 退出码 {exit_code}：{_failure_reason(log_path)}"
    elif failure is not None:
        detail = failure

    result: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": time.time(),
        "elapsed_s": time.time() - started_at,
        "commands": commands,
        "exit_code": exit_code,
        "ok": ok,
        "converged": converged,
        "timed_out": not converged and failure is None and exit_code == 0,
        "generation_id": snapshot.generation_id,
        "generation": snapshot.generation,
        "phase": snapshot.phase,
        "ready": snapshot.ready,
        "total": snapshot.total,
        "not_ready": snapshot.not_ready,
        "detail": detail,
        "config_error": snapshot.config_error or "",
        "log": str(log_path),
        "session_id": session_id or pick_session(state_dir),
        "notified": False,
    }
    if notify_wanted:
        result["notified"] = notify(
            result, state_dir=state_dir, session_id=result["session_id"]
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    result_path(state_dir).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok else 1


def detach(
    *,
    commands: list[str],
    state_dir: Path,
    log_path: Path,
    wait: float,
    notify_wanted: bool,
    session_id: str | None,
) -> int:
    """父进程：fork 出独立会话执行工作，自己立刻返回。"""
    child = os.fork()
    if child > 0:
        print(f"已派发（pid={child}）：{' → '.join(commands)}；日志 {log_path}")
        print("重启期间工具进程可能被一起重启；完成后会往当前会话回一条消息。")
        return 0
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        # 子进程的 stdout/stderr 也丢进日志，别依赖已关闭的管道
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        for name in _INHERITED_ENV:
            os.environ.pop(name, None)
        code = run_worker(
            commands=commands,
            state_dir=state_dir,
            log_path=log_path,
            wait=wait,
            notify_wanted=notify_wanted,
            session_id=session_id,
        )
    except BaseException:  # 子进程里必须自己收尾，别把异常抛回父进程
        os._exit(1)
    os._exit(code)


def _read_result(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload: object = json.loads(result_path(state_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return (
        {str(k): v for k, v in payload.items()} if isinstance(payload, dict) else None
    )


def _print_status(state_dir: Path) -> int:
    _, snapshot = read_state(state_dir)
    state = "已收敛" if snapshot.converged else "未收敛"
    print(
        f"{state}：世代 {_fmt_generation(snapshot.generation_id)} "
        f"{snapshot.phase or '-'}，{snapshot.ready}/{snapshot.total} Ready"
    )
    if snapshot.not_ready:
        print(f"未就绪：{'、'.join(snapshot.not_ready)}")
    failure = snapshot.terminal_failure
    if failure:
        print(f"原因：{failure}")
    return 0 if snapshot.converged else 1


def _print_report(state_dir: Path) -> int:
    result = _read_result(state_dir)
    if result is None:
        print("还没有重启记录（先跑一次 apply）")
        return 1
    print(render_message(result))
    print(f"exit={result.get('exit_code')} converged={result.get('converged')}")
    return 0 if result.get("ok") else 1


def main(argv: Sequence[str] | None = None) -> int:
    """入口。"""
    parser = argparse.ArgumentParser(
        prog="blue_restart", description="分离执行重启，收敛后给会话回一条消息"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="apply",
        choices=["apply", "down-up", "report", "status"],
        help="apply（默认）/ down-up / report / status",
    )
    parser.add_argument("--wait", type=float, default=300.0, help="等收敛的秒数")
    parser.add_argument("--state-dir", default=None, help="缺省 .blue")
    parser.add_argument("--session", default=None, help="回消息的目标会话 id")
    parser.add_argument(
        "--no-notify", action="store_true", help="只写结果文件，不回消息"
    )
    args = parser.parse_args(argv)

    state_dir = (
        Path(args.state_dir).expanduser().resolve()
        if args.state_dir
        else PROJECT_ROOT / ".blue"
    )
    if args.action == "report":
        return _print_report(state_dir)
    if args.action == "status":
        return _print_status(state_dir)

    commands = _COMMANDS[args.action]
    log_path = Path(f"/tmp/blue-restart-{args.action}-{int(time.time())}.log")
    return detach(
        commands=commands,
        state_dir=state_dir,
        log_path=log_path,
        wait=float(args.wait),
        notify_wanted=not bool(args.no_notify),
        session_id=args.session,
    )


if __name__ == "__main__":
    raise SystemExit(main())
