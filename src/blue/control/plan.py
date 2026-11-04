"""apply 之前的变更预览：谁会新增、重启、删除，以及为什么。

对标 ``kubectl diff`` / ``terraform plan``。判断逻辑与调和器共用
:func:`restart_reasons`，避免"预览说不动、apply 却重启"的两套标准。

信息全部来自两份快照，不需要 supervisor 在跑：

- ``DesiredState``：刚编译出来的 ``blue.infd``（含 config/source/契约指纹）
- ``ActualState``：``.blue/state.json`` 里上一轮调谐的结果

已知边界：``.blue/state.json`` 的 ``ActualService`` 只记录 config/source/版本
指纹，不记录服务清单（kind/depends_on 等）。所以「只改了 service.infd 的
依赖关系、恰好没改 config 与源码」时会漏报，:attr:`PlanResult.note` 会说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from blue.control.model import (
    ActualState,
    DesiredService,
    DesiredState,
    ServiceMeta,
    ServiceStatus,
)

__all__ = [
    "ContractChange",
    "PlanResult",
    "ServiceChange",
    "changed_contracts",
    "plan",
    "restart_reasons",
]

Action = Literal["create", "restart", "remove", "keep"]
"""一个服务在本次 apply 中的命运。"""

_UNHEALTHY = frozenset(
    {ServiceStatus.FAILED, ServiceStatus.STOPPED, ServiceStatus.PENDING}
)
"""这些状态的服务即使配置没变也要重新拉起。"""


@dataclass(frozen=True)
class ServiceChange:
    """单个服务的变更。"""

    name: str
    action: Action
    reasons: tuple[str, ...] = ()
    status: str = ""

    @property
    def changed(self) -> bool:
        """是否真的会动这个服务。"""
        return self.action != "keep"


@dataclass(frozen=True)
class ContractChange:
    """单个契约的变更与受影响的服务。"""

    name: str
    action: Literal["create", "remove", "change"]
    before: str = ""
    after: str = ""
    affected: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanResult:
    """一次 apply 的完整预览。"""

    desired_generation: str
    actual_generation: str | None
    services: tuple[ServiceChange, ...]
    contracts: tuple[ContractChange, ...]
    note: str = ""

    @property
    def has_changes(self) -> bool:
        """是否有任何服务会被新增/重启/删除（契约本身不单独生效）。"""
        return any(change.changed for change in self.services)

    def counts(self) -> dict[str, int]:
        """按动作统计。"""
        tally = {"create": 0, "restart": 0, "remove": 0, "keep": 0}
        for change in self.services:
            tally[change.action] += 1
        return tally

    @property
    def restarted(self) -> tuple[str, ...]:
        """会被重启/新增的服务名（apply 期间它们会短暂不可用）。"""
        return tuple(
            change.name for change in self.services if change.action == "restart"
        )


def restart_reasons(
    *,
    current: DesiredService,
    previous_meta: ServiceMeta | None,
    previous_config_hash: str,
    previous_source_hash: str,
    previous_version: str,
    changed_contracts: set[str],
) -> tuple[str, ...]:
    """判断一个服务为什么需要（重新）启动；返回空元组表示不用动。

    调和器与 :func:`plan` 共用本函数：前者用内存里的上一份期望配置，
    后者用 ``.blue/state.json`` 的快照；``previous_meta`` 为 ``None`` 时
    （快照里没有服务清单）跳过清单比较。
    """
    reasons: list[str] = []
    if previous_meta is not None and previous_meta != current.meta:
        reasons.append("服务清单变化")
    if previous_config_hash != current.config_hash:
        reasons.append("配置变化")
    if previous_source_hash != current.source_hash:
        reasons.append("源码变化")
    if previous_version != current.package_version:
        reasons.append("版本变化")
    affected = sorted(set(current.meta.depends_contracts) & changed_contracts)
    if affected:
        reasons.append(f"依赖契约变化：{', '.join(affected)}")
    return tuple(reasons)


def changed_contracts(
    desired: DesiredState, actual: ActualState | None
) -> dict[str, tuple[str, str]]:
    """返回发生变化的契约：``{名字: (旧指纹, 新指纹)}``。

    ``ActualContract`` 只记录包版本与源码指纹，因此这里也只比较这两项；
    契约新增时旧指纹为空串。
    """
    changes: dict[str, tuple[str, str]] = {}
    previous = actual.contracts if actual is not None else {}
    for name, contract in desired.contracts.items():
        old = previous.get(name)
        old_fingerprint = (
            f"{old.package_version}:{old.source_hash}" if old is not None else ""
        )
        new_fingerprint = f"{contract.package_version}:{contract.source_hash}"
        if old_fingerprint != new_fingerprint:
            changes[name] = (old_fingerprint, new_fingerprint)
    for name in set(previous) - set(desired.contracts):
        old = previous[name]
        changes[name] = (f"{old.package_version}:{old.source_hash}", "")
    return changes


def plan(desired: DesiredState, actual: ActualState | None) -> PlanResult:
    """对比期望状态与实际快照，得出本次 apply 会做什么。

    :param desired: 刚编译出来的期望状态
    :param actual: ``.blue/state.json`` 的快照；``None`` 表示还没部署过
    """
    contract_changes = changed_contracts(desired, actual)
    touched = set(contract_changes)
    services: list[ServiceChange] = []

    for name, service in sorted(desired.services.items()):
        previous = actual.services.get(name) if actual is not None else None
        if previous is None:
            services.append(
                ServiceChange(name=name, action="create", reasons=("新增",))
            )
            continue
        reasons = list(
            restart_reasons(
                current=service,
                previous_meta=None,
                previous_config_hash=previous.config_hash,
                previous_source_hash=previous.source_hash,
                previous_version=previous.package_version,
                changed_contracts=touched,
            )
        )
        if previous.status in _UNHEALTHY:
            reasons.append(f"当前状态 {previous.status.value}")
        if reasons:
            services.append(
                ServiceChange(
                    name=name,
                    action="restart",
                    reasons=tuple(reasons),
                    status=previous.status.value,
                )
            )
        else:
            services.append(
                ServiceChange(name=name, action="keep", status=previous.status.value)
            )

    if actual is not None:
        for name in sorted(set(actual.services) - set(desired.services)):
            services.append(
                ServiceChange(name=name, action="remove", reasons=("已移除",))
            )

    contracts = tuple(
        ContractChange(
            name=name,
            action="create" if not before else ("remove" if not after else "change"),
            before=before,
            after=after,
            affected=tuple(
                sorted(
                    service_name
                    for service_name, service in desired.services.items()
                    if name in service.meta.depends_contracts
                )
            ),
        )
        for name, (before, after) in sorted(contract_changes.items())
    )

    note = ""
    if actual is None:
        note = "没有找到 .blue/state.json（还没部署过），所有服务都会新建。"
    else:
        note = (
            "预览基于 state.json 快照：服务清单（kind/depends_on 等）的变化"
            "无法从快照还原，若只改了这些字段，apply 可能比预览多重启几个服务。"
        )
    return PlanResult(
        desired_generation=desired.generation,
        actual_generation=actual.generation if actual is not None else None,
        services=tuple(services),
        contracts=contracts,
        note=note,
    )
