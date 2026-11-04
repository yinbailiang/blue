"""``blue plan`` 的变更预览测试：新增 / 重启 / 删除 / 不变 的判定。

用仓库真实的 ``blue.infd`` 编译出期望状态，再手工构造 ``.blue/state.json``
快照——这样测试不会和配置结构脱节。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blue.config import load_desired
from blue.control.model import (
    ActualContract,
    ActualService,
    ActualState,
    DesiredState,
    ServiceStatus,
)
from blue.control.plan import plan, restart_reasons

ROOT = Path(__file__).resolve().parents[1]
"""项目根（含 blue.infd）。"""


@pytest.fixture(scope="module")
def desired() -> DesiredState:
    """编译一次真实配置。"""
    return load_desired(ROOT / "blue.infd")


def _actual(desired: DesiredState, *, generation: str = "old") -> ActualState:
    """按期望状态伪造一份「刚部署完、全部 Ready」的快照。"""
    return ActualState(
        generation=generation,
        supervisor_pid=1234,
        started_at=0.0,
        updated_at=0.0,
        services={
            name: ActualService(
                name=name,
                kind=service.meta.kind,
                status=ServiceStatus.READY,
                config_hash=service.config_hash,
                package_version=service.package_version,
                source_hash=service.source_hash,
            )
            for name, service in desired.services.items()
        },
        contracts={
            name: ActualContract(
                name=name,
                package=contract.meta.package,
                module=contract.meta.module,
                package_version=contract.package_version,
                source_hash=contract.source_hash,
            )
            for name, contract in desired.contracts.items()
        },
    )


def test_no_changes_means_clean_plan(desired: DesiredState) -> None:
    """快照与期望一致时，一个服务都不动。"""
    result = plan(desired, _actual(desired))

    assert result.has_changes is False
    assert result.counts()["keep"] == len(desired.services)
    assert result.restarted == ()
    assert result.contracts == ()


def test_source_change_triggers_restart(desired: DesiredState) -> None:
    """源码指纹变化 → 该服务重启，原因写清楚。"""
    actual = _actual(desired)
    name = "agent"
    actual.services[name].source_hash = "0" * 64

    result = plan(desired, actual)

    change = next(item for item in result.services if item.name == name)
    assert change.action == "restart"
    assert "源码变化" in change.reasons
    assert name in result.restarted
    assert result.has_changes is True


def test_config_change_is_distinguished_from_source(desired: DesiredState) -> None:
    """配置变化与源码变化分别标注，方便判断该看哪份文件。"""
    actual = _actual(desired)
    name = "session"
    actual.services[name].config_hash = "deadbeef"

    change = next(item for item in plan(desired, actual).services if item.name == name)

    assert change.action == "restart"
    assert change.reasons == ("配置变化",)


def test_contract_change_restarts_dependents(desired: DesiredState) -> None:
    """契约变化 → 依赖它的服务全部重启，原因里点出契约名。"""
    actual = _actual(desired)
    contract = next(iter(desired.contracts))
    actual.contracts[contract].source_hash = "1" * 64

    result = plan(desired, actual)

    affected = [
        change
        for change in result.services
        if change.action == "restart"
        and any("依赖契约变化" in reason for reason in change.reasons)
    ]
    assert affected, "依赖该契约的服务应该被重启"
    assert all(contract in " ".join(change.reasons) for change in affected)
    assert result.contracts[0].name == contract


def test_new_and_removed_services(desired: DesiredState) -> None:
    """新增的服务标 create，快照里多出来的标 remove。"""
    actual = _actual(desired)
    # 快照里多一个期望中不存在的服务 → remove
    ghost = "ghost_service"
    assert ghost not in desired.services
    kind = next(iter(desired.services.values())).meta.kind
    actual.services[ghost] = ActualService(
        name=ghost, kind=kind, status=ServiceStatus.READY
    )
    # 期望里挑一个快照中抹掉 → create
    missing = next(iter(desired.services))
    del actual.services[missing]

    result = plan(desired, actual)

    actions = {change.name: change.action for change in result.services}
    assert actions[missing] == "create"
    assert actions[ghost] == "remove"
    tally = result.counts()
    assert tally["create"] == 1
    assert tally["remove"] == 1


def test_unhealthy_service_is_restarted_even_without_changes(
    desired: DesiredState,
) -> None:
    """配置没变但服务处于失败状态时，apply 会重新拉起。"""
    actual = _actual(desired)
    name = "clock"
    actual.services[name].status = ServiceStatus.FAILED

    change = next(item for item in plan(desired, actual).services if item.name == name)

    assert change.action == "restart"
    assert any("当前状态" in reason for reason in change.reasons)


def test_missing_state_means_everything_is_created(desired: DesiredState) -> None:
    """没有 state.json（首次部署）时全部按新增处理。"""
    result = plan(desired, None)

    assert result.actual_generation is None
    assert result.counts()["create"] == len(desired.services)
    assert "还没部署过" in result.note


def test_restart_reasons_shared_with_reconciler(desired: DesiredState) -> None:
    """共用判定函数：清单/配置/源码/版本/契约任一变化都算需要重启。"""
    service = next(
        item for item in desired.services.values() if item.meta.depends_contracts
    )

    assert (
        restart_reasons(
            current=service,
            previous_meta=service.meta,
            previous_config_hash=service.config_hash,
            previous_source_hash=service.source_hash,
            previous_version=service.package_version,
            changed_contracts=set(),
        )
        == ()
    )

    reasons = restart_reasons(
        current=service,
        previous_meta=None,
        previous_config_hash="x",
        previous_source_hash="y",
        previous_version="z",
        changed_contracts=set(service.meta.depends_contracts),
    )

    assert "配置变化" in reasons
    assert "源码变化" in reasons
    assert "版本变化" in reasons
    assert any("依赖契约变化" in reason for reason in reasons)
