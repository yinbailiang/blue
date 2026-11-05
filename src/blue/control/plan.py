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

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import unified_diff
from typing import Any, Literal, cast

from infinity_data.emit import yaml_dump

from blue.control.desired import redact_config
from blue.control.model import (
    ActualState,
    DesiredSdkPackage,
    DesiredService,
    DesiredState,
    ServiceMeta,
    ServiceStatus,
)

__all__ = [
    "ContractChange",
    "PlanResult",
    "SdkChange",
    "ServiceChange",
    "changed_contracts",
    "changed_packages",
    "changed_sdk_packages",
    "config_diff",
    "plan",
    "restart_reasons",
    "sdk_changes",
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
    config_diff: tuple[str, ...] = ()
    """配置的字段级变化（仅在 ``config_hash`` 变了、且旧快照有配置内容时给出）。"""

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
class SdkChange:
    """单个 SDK 运行时包（``sdk/*``）的变更与受影响的服务。"""

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
    sdk: tuple[SdkChange, ...] = ()
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


DIFF_LIMIT = 24
"""一份 diff 最多展示多少行（其余折叠成一行提示）。"""


def config_diff(
    previous: dict[str, Any], current: dict[str, Any], *, limit: int = DIFF_LIMIT
) -> tuple[str, ...]:
    """对比两份配置，返回统一 diff（``-`` 旧值 / ``+`` 新值 / ``@@`` 区块头）。

    渲染交给 ``infinity_data.emit.yaml_dump``：infd 编译出来的浮点是
    ``Decimal``，它按 infd 的写法输出（``300.0``），嵌套结构与列表也是原生
    格式——比手写序列化可靠，也省得自己处理各种边界值。

    ``current`` 在比较前会先脱敏：密钥类字段两边都是 ``***``，因此"只有密钥
    变了"的情况会退化成一行说明，而不是把新密钥打印出来。
    """
    redacted = redact_config(current)
    before = yaml_dump(_sorted(previous)).splitlines()
    after = yaml_dump(_sorted(redacted)).splitlines()
    if before == after:
        return ()
    lines: list[str] = []
    for line in unified_diff(before, after, lineterm="", n=1):
        if line.startswith(("---", "+++")):
            continue
        # unified_diff 的标记紧贴内容（"-foo"），补一个空格更像 kubectl diff
        lines.append(f"{line[0]} {line[1:]}" if line[:1] in "+-" else line)
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = [*lines[:limit], f"… 还有 {extra} 行变化"]
    return tuple(lines)


def _sorted(value: object) -> Any:
    """递归按键名排序，消除字典顺序带来的假差异。"""
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _sorted(mapping[key]) for key in sorted(mapping, key=str)}
    if isinstance(value, list):
        return [_sorted(item) for item in cast(list[object], value)]
    return value


def restart_reasons(
    *,
    current: DesiredService,
    previous_meta: ServiceMeta | None,
    previous_config_hash: str,
    previous_source_hash: str,
    previous_version: str,
    changed_contracts: set[str],
    sdk_changed: bool = False,
    changed_packages: set[str] | None = None,
    previous_artifact_hash: str = "",
) -> tuple[str, ...]:
    """判断一个服务为什么需要（重新）启动；返回空元组表示不用动。

    调和器与 :func:`plan` 共用本函数：前者用内存里的上一份期望配置，
    后者用 ``.blue/state.json`` 的快照；``previous_meta`` 为 ``None`` 时
    （快照里没有服务清单）跳过清单比较。

    ``previous_artifact_hash`` 是**兜底判据**：产物指纹里有 ``uv.lock`` 与解释器
    版本这两个维度，它们变化时上面六项（清单/配置/源码/版本/契约/SDK）全都不变，
    但服务必须重启——否则进程继续跑旧产物，新世代声明的 ``artifact_hash`` 与实际
    运行的对不上，依赖升级永远不生效。
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
    if sdk_changed:
        # blue 核心是每个服务的隐式运行时依赖（runtime/transport 都在里面）
        reasons.append("SDK 变化")
    affected_packages = sorted(
        set(current.meta.depends_sdk) & (changed_packages or set())
    )
    if affected_packages:
        reasons.append(f"依赖 SDK 变化：{', '.join(affected_packages)}")
    if (
        previous_artifact_hash
        and current.artifact_hash
        and previous_artifact_hash != current.artifact_hash
    ):
        reasons.append("产物变化（依赖锁/解释器等）")
    return tuple(reasons)


def sdk_changed(desired: DesiredState, actual: ActualState | None) -> bool:
    """blue **核心**是否变了（它不进 depends_contracts，但每个服务都依赖它）。

    ``sdk/*`` 运行时包不在这个判据里：它们只影响声明了 ``depends_sdk``
    的服务，由 :func:`changed_sdk_packages` 单独判断。

    ``actual.sdk is None`` 说明快照是老格式写的——此时无法判断，按"变了"处理，
    宁可多重启一次也不要让服务悄悄跑旧代码。
    """
    if actual is None or actual.sdk is None:
        return True
    return (
        actual.sdk.package_version,
        actual.sdk.source_hash,
    ) != (desired.sdk.package_version, desired.sdk.source_hash)


def changed_packages(
    previous: Mapping[str, DesiredSdkPackage] | None,
    current: Mapping[str, DesiredSdkPackage],
) -> set[str]:
    """返回发生变化的 ``sdk/*`` 运行时包名。

    :param previous: 上一份包指纹；``None`` 表示老快照/新增，按全部包都变了处理
    """
    if previous is None:
        return set(current)
    changes: set[str] = set()
    for name, package in current.items():
        old = previous.get(name)
        if old is None or (
            old.package_version,
            old.source_hash,
        ) != (package.package_version, package.source_hash):
            changes.add(name)
    changes.update(set(previous) - set(current))
    return changes


def changed_sdk_packages(desired: DesiredState, actual: ActualState | None) -> set[str]:
    """从实际状态快照判断哪些运行时包变了。"""
    return {change.name for change in sdk_changes(desired, actual)}


def sdk_changes(
    desired: DesiredState, actual: ActualState | None
) -> tuple[SdkChange, ...]:
    """返回 SDK 运行时包的变更（新增/删除/指纹变化）。

    与契约变更一样，SDK 包自身不单独生效；真正重启的是
    ``depends_sdk`` 里声明了它的服务（见 :func:`restart_reasons`）。
    """
    previous = actual.sdk.packages if actual is not None and actual.sdk else {}
    affected = {
        name: tuple(
            sorted(
                service_name
                for service_name, service in desired.services.items()
                if name in service.meta.depends_sdk
            )
        )
        for name in set(previous) | set(desired.sdk.packages)
    }
    changes: list[SdkChange] = []
    for name in sorted(set(previous) | set(desired.sdk.packages)):
        old = previous.get(name)
        new = desired.sdk.packages.get(name)
        if old is None and new is not None:
            changes.append(
                SdkChange(name, "create", "", new.source_hash, affected[name])
            )
        elif new is None and old is not None:
            changes.append(
                SdkChange(name, "remove", old.source_hash, "", affected[name])
            )
        elif (
            old is not None
            and new is not None
            and (old.package_version, old.source_hash)
            != (new.package_version, new.source_hash)
        ):
            changes.append(
                SdkChange(
                    name, "change", old.source_hash, new.source_hash, affected[name]
                )
            )
    return tuple(changes)


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

    sdk_touched = sdk_changed(desired, actual)
    sdk_package_changes = sdk_changes(desired, actual)
    packages_touched = {change.name for change in sdk_package_changes}
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
                sdk_changed=sdk_touched,
                changed_packages=packages_touched,
                previous_artifact_hash=previous.artifact_hash,
            )
        )
        if previous.status in _UNHEALTHY:
            reasons.append(f"当前状态 {previous.status.value}")
        if reasons:
            field_diff: tuple[str, ...] = ()
            if "配置变化" in reasons and previous.config:
                # 旧快照可能是升级前写的（没有 config 字段），那就只报告哈希变化
                field_diff = config_diff(previous.config, redact_config(service.config))
            services.append(
                ServiceChange(
                    name=name,
                    action="restart",
                    reasons=tuple(reasons),
                    status=previous.status.value,
                    config_diff=field_diff,
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
        sdk=sdk_package_changes,
        note=note,
    )
