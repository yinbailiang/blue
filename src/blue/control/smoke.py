"""激活后的冒烟检查：验证「行为对」，而不只是「进程起来了」。

健康闸门只看就绪探针。一个能启动、探针通过、但工具全没注册成功的世代照样会被
标成 ``stable``——而自改进最容易造出的就是这种「半对不对」的错（本项目实测过：
改前端资源不生效、服务一直是 Ready）。

**每个服务自己声明自己怎么被检查**（写在自己的 ``service.infd`` 里）：

- ``smoke_tools``：这些工具名必须出现在工具注册表里（最精确）
- ``smoke_min_tools``：至少要注册几个工具（零维护，专抓「一个都没注册」）
- ``smoke_command``：自定义检查命令，退出码 0 视为通过。首元素 ``python`` 会被
  替换成**该服务产物 venv 的解释器**，cwd 是项目根，环境里带本世代的
  ``BLUE_CONFIG_FILE``——于是检查跑的就是这个世代真正部署的代码。

控制面只负责执行与汇总，**不含任何业务知识**：session 的写读检查写在 session
自己的包里，weather 只需声明 ``smoke_tools = ["get_weather"]``。
没声明的服务直接跳过（显式优于默认拦截）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from blue.control.artifacts import ArtifactStore
from blue.control.model import DesiredService, DesiredState

__all__ = ["Check", "SmokeReport", "run_smoke"]

_PYTHON_ALIASES = frozenset({"python", "python3"})
_MAX_DETAIL = 400
"""失败详情保留多少字符（stdout/stderr 尾部）。"""


@dataclass(frozen=True)
class Check:
    """一条冒烟结果。"""

    service: str
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class SmokeReport:
    """一次冒烟的汇总。"""

    checks: tuple[Check, ...] = ()
    tools: tuple[str, ...] = ()
    """观测到的工具名（落进 ``.stable`` 供诊断，不参与判定）。"""

    @property
    def ok(self) -> bool:
        """全部通过（没有任何声明时也算通过）。"""
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        """失败的条目。"""
        return tuple(check for check in self.checks if not check.ok)

    @property
    def declared(self) -> int:
        """有多少个服务声明了冒烟。"""
        return len({check.service for check in self.checks})

    def summary(self) -> str:
        """给人看的一行结论（含检查类型分布，便于确认探针真的跑了）。"""
        if not self.checks:
            return "没有服务声明冒烟检查"
        passed = len(self.checks) - len(self.failures)
        kinds = Counter(check.name for check in self.checks)
        breakdown = "、".join(
            f"{name}×{count}" for name, count in sorted(kinds.items())
        )
        head = (
            f"{passed}/{len(self.checks)} 通过（{breakdown}，{self.declared} 个服务）"
        )
        if self.ok:
            return head
        details = "；".join(
            f"{check.service}.{check.name}"
            + (f"（{check.detail}）" if check.detail else "")
            for check in self.failures
        )
        return f"{head}，失败：{details}"


@dataclass
class _Plan:
    """一个服务的冒烟声明（执行期的中间表示）。"""

    service: DesiredService
    tools: tuple[str, ...] = ()
    min_tools: int = 0
    command: tuple[str, ...] = ()
    checks: list[Check] = field(default_factory=list[Check])


def _declared(desired: DesiredState) -> list[_Plan]:
    plans: list[_Plan] = []
    for service in desired.services.values():
        meta = service.meta
        declared = meta.smoke_tools or meta.smoke_min_tools > 0 or meta.smoke_command
        if not declared:
            continue
        plans.append(
            _Plan(
                service=service,
                tools=meta.smoke_tools,
                min_tools=meta.smoke_min_tools,
                command=meta.smoke_command,
            )
        )
    return plans


async def run_smoke(
    desired: DesiredState, *, artifacts: ArtifactStore, timeout: float | None = None
) -> SmokeReport:
    """按服务声明跑一遍冒烟。

    :param desired: 目标世代重建出来的期望状态
    :param artifacts: 产物仓库（定位服务自己的解释器）
    :param timeout: 覆盖总线请求超时（命令超时由 ``service.infd`` 各自声明）
    :return: 汇总；``report.ok`` 表示是否全部通过
    """
    plans = _declared(desired)
    if not plans:
        return SmokeReport()

    wants_tools = [plan for plan in plans if plan.tools or plan.min_tools > 0]
    names: frozenset[str] | None = None
    catalog_error = ""
    if wants_tools:
        names, catalog_error = await _query_tools(
            desired, timeout=timeout if timeout is not None else 10.0
        )

    checks: list[Check] = []
    for plan in plans:
        if plan.tools or plan.min_tools > 0:
            checks.extend(_check_tools(plan, names, catalog_error))
        if plan.command:
            checks.append(
                await _run_command(desired, plan.service, artifacts, timeout=timeout)
            )

    return SmokeReport(
        checks=tuple(checks), tools=tuple(sorted(names)) if names else ()
    )


def _check_tools(
    plan: _Plan, names: frozenset[str] | None, catalog_error: str
) -> list[Check]:
    """核对工具声明（目录整体查不到就是失败，不是「没声明」）。"""
    service = plan.service.meta.name
    if names is None:
        return [
            Check(
                service=service,
                name="smoke_tools",
                ok=False,
                detail=catalog_error or "工具目录不可查",
            )
        ]
    checks: list[Check] = []
    present = 0
    if plan.tools:
        missing = [tool for tool in plan.tools if tool not in names]
        present = len(plan.tools) - len(missing)
        checks.append(
            Check(
                service=service,
                name="smoke_tools",
                ok=not missing,
                detail=f"缺少工具：{', '.join(missing)}" if missing else "",
            )
        )
    if plan.min_tools > 0:
        # 列了具体名字就按列出的计；只声明数量时退化成看全局目录（抓"全军覆没"）
        observed = present if plan.tools else len(names)
        checks.append(
            Check(
                service=service,
                name="smoke_min_tools",
                ok=observed >= plan.min_tools,
                detail=(
                    ""
                    if observed >= plan.min_tools
                    else f"只登记了 {observed} 个，至少要 {plan.min_tools}"
                ),
            )
        )
    return checks


async def _query_tools(
    desired: DesiredState, *, timeout: float
) -> tuple[frozenset[str] | None, str]:
    """查一次工具目录；失败时返回 ``(None, 原因)``。"""
    try:
        from blue_contracts_tool import (
            CatalogQueryEvent,
            CatalogQueryResponse,
            CatalogReplyEvent,
        )
        from infinity_bus.templates.request import request

        from blue.runtime import build_runtime
    except ImportError as error:  # 缺包就别把整个 apply 卡死
        return None, f"导入契约失败：{error}"

    role = "agent.smoke"
    previous = os.environ.get("BLUE_SERVICE_NAME")
    os.environ["BLUE_SERVICE_NAME"] = role
    runtime = build_runtime(
        role, events=[CatalogQueryEvent, CatalogReplyEvent], url=desired.bus_url
    )
    try:
        await runtime.start()
        proxy = runtime.proxy(role)
        response = await request(
            proxy,
            CatalogQueryEvent.name,
            {},
            CatalogReplyEvent.name,
            timeout=timeout,
        )
        if not isinstance(response, CatalogQueryResponse):
            return None, f"收到非预期的目录响应：{type(response).__name__}"
        return frozenset(tool.function.name for tool in response.tools), ""
    except Exception as error:  # 总线/注册中心不可用：冒烟失败而不是崩溃
        return None, f"工具目录查询失败：{error}"
    finally:
        await runtime.stop()
        if previous is None:
            os.environ.pop("BLUE_SERVICE_NAME", None)
        else:
            os.environ["BLUE_SERVICE_NAME"] = previous


def _command_for(service: DesiredService, artifacts: ArtifactStore) -> list[str]:
    """把声明里的 ``python`` 换成该服务产物的解释器。"""
    command = list(service.meta.smoke_command)
    if not command:
        return command
    if command[0] in _PYTHON_ALIASES:
        interpreter: Path | None = service.artifact_python
        if interpreter is None and service.artifact_hash:
            ref = artifacts.resolve(service.meta.name, service.artifact_hash)
            interpreter = ref.python if ref.complete else None
        command[0] = str(interpreter) if interpreter is not None else sys.executable
    return command


async def _run_command(
    desired: DesiredState,
    service: DesiredService,
    artifacts: ArtifactStore,
    *,
    timeout: float | None,
) -> Check:
    """跑服务自己声明的检查命令。"""
    name = service.meta.name
    command = _command_for(service, artifacts)
    executable = shutil.which(command[0]) if os.sep not in command[0] else command[0]
    if executable is None:
        return Check(
            service=name,
            name="smoke_command",
            ok=False,
            detail=f"命令不存在：{command[0]}",
        )

    environment = dict(os.environ)
    environment.update(service.meta.env)
    environment.update(
        {
            "BLUE_SERVICE_NAME": name,
            "BLUE_INSTANCE_ID": f"smoke-{name}",
            "BLUE_CONFIG_FILE": str(service.config_path),
            "BLUE_ARTIFACT_HASH": service.artifact_hash,
            "BLUE_SMOKE": "1",
        }
    )
    workdir = service.cwd or desired.base_dir
    limit = timeout if timeout is not None else service.meta.smoke_timeout
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *command[1:],
            cwd=workdir,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=limit
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return Check(
                service=name,
                name="smoke_command",
                ok=False,
                detail=f"超时（{limit:.0f}s）：{' '.join(command)}",
            )
    except OSError as error:
        return Check(
            service=name, name="smoke_command", ok=False, detail=f"启动失败：{error}"
        )

    if process.returncode == 0:
        return Check(service=name, name="smoke_command", ok=True)
    output = (stdout + stderr).decode("utf-8", "replace").strip()
    return Check(
        service=name,
        name="smoke_command",
        ok=False,
        detail=f"退出码 {process.returncode}：{_tail(output)}",
    )


def _tail(text: str) -> str:
    """失败输出只留尾部几行，避免把日志灌进 apply 的输出。"""
    lines = [line for line in text.splitlines() if line.strip()]
    joined = " / ".join(lines[-3:])[:_MAX_DETAIL]
    return joined or "（无输出）"
