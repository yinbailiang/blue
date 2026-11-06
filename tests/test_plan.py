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
    ActualSdk,
    ActualService,
    ActualState,
    DesiredSdkPackage,
    DesiredState,
    SdkMeta,
    ServiceStatus,
)
from blue.control.plan import config_diff, plan, restart_reasons

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
                artifact_hash=service.artifact_hash,
                meta=service.meta,
            )
            for name, service in desired.services.items()
        },
        sdk=ActualSdk(
            package_version=desired.sdk.package_version,
            source_hash=desired.sdk.source_hash,
            packages=desired.sdk.packages,
        ),
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


def test_config_diff_reports_added_removed_changed() -> None:
    """统一 diff：新增、删除、修改都能看出来（渲染走 infd 的 yaml_dump）。"""
    before = {"llm_timeout": 60.0, "model": "old", "gone": 1}
    after = {"llm_timeout": 300.0, "model": "old", "fresh": {"nested": True}}

    lines = config_diff(before, after)

    assert any(line.startswith("- llm_timeout: 60.0") for line in lines)
    assert any(line.startswith("+ llm_timeout: 300.0") for line in lines)
    assert any(line.startswith("- gone: 1") for line in lines)
    assert any("fresh" in line and line.startswith("+") for line in lines)
    # 没变的字段不出现在变更行里
    assert not any("model" in line and line.startswith(("+ ", "- ")) for line in lines)


def test_config_diff_keeps_secrets_redacted() -> None:
    """密钥类字段被脱敏：diff 里不会出现明文。"""
    lines = config_diff({"api_key": "***"}, {"api_key": "sk-live-secret"})

    # 脱敏后两边一致 → 纯 diff 为空；plan 层会补一句"仅密钥字段有变化"
    assert lines == ()
    assert not any("sk-live-secret" in line for line in lines)


def test_config_diff_truncates_long_changes() -> None:
    """变化太多时折叠，避免刷屏。"""
    before = {f"k{index}": index for index in range(40)}
    after = {f"k{index}": index + 1 for index in range(40)}

    lines = config_diff(before, after)

    assert 0 < len(lines) <= 25
    last = next(reversed(lines))
    assert last.startswith("… 还有")


def test_config_diff_ignores_key_order() -> None:
    """字典顺序不同不算变化（渲染前递归排序）。"""
    assert config_diff({"b": 1, "a": 2}, {"a": 2, "b": 1}) == ()


def test_plan_includes_field_diff_when_snapshot_has_config(
    desired: DesiredState,
) -> None:
    """旧快照带着配置内容时，restart 会附上字段级变化。"""
    actual = _actual(desired)
    name = "agent"
    service = actual.services[name]
    service.config_hash = "changed"
    service.config = {"llm_timeout": 60.0, "model": "old-model"}

    change = next(item for item in plan(desired, actual).services if item.name == name)

    assert change.action == "restart"
    joined = " ".join(change.config_diff)
    assert "llm_timeout" in joined


def test_plan_omits_diff_when_snapshot_has_no_config(
    desired: DesiredState,
) -> None:
    """升级前写下的快照没有 config 字段：只报告哈希变化，不瞎猜字段。"""
    actual = _actual(desired)
    name = "agent"
    actual.services[name].config_hash = "changed"
    assert actual.services[name].config == {}

    change = next(item for item in plan(desired, actual).services if item.name == name)

    assert change.action == "restart"
    assert change.config_diff == ()


def test_sdk_change_restarts_every_service(desired: DesiredState) -> None:
    """blue 核心变了（runtime/transport 在里面）→ 所有服务都要重启。

    这是调谐器原来的盲区：服务包的 source_hash 只覆盖 services/<name>/，
    SDK 改了完全察觉不到，进程会一直跑旧代码。
    """
    snapshot = _actual(desired)
    snapshot.sdk = ActualSdk(package_version="0.0.1", source_hash="deadbeef")
    result = plan(desired, snapshot)
    assert result.counts()["restart"] == len(desired.services)
    for change in result.services:
        assert "SDK 变化" in change.reasons


def test_runtime_package_change_restarts_only_dependents(
    desired: DesiredState,
) -> None:
    """``sdk/*`` 运行时包变化只重启声明了 ``depends_sdk`` 的服务。"""
    snapshot = _actual(desired)
    assert snapshot.sdk is not None
    snapshot.sdk.packages["tool"] = DesiredSdkPackage(
        meta=SdkMeta(name="tool", package="blue-tool-sdk", module="blue_tool_sdk"),
        package_version="0.1.0",
        source_hash="old-hash",
    )

    result = plan(desired, snapshot)

    dependents = {
        name
        for name, service in desired.services.items()
        if "tool" in service.meta.depends_sdk
    }
    restarted = {
        change.name for change in result.services if change.action == "restart"
    }
    assert dependents
    assert restarted == dependents
    for change in result.services:
        if change.name in dependents:
            assert any("依赖 SDK 变化" in reason for reason in change.reasons)

    sdk_change = next(change for change in result.sdk if change.name == "tool")
    assert sdk_change.action == "change"
    assert sdk_change.before == "old-hash"
    assert sdk_change.after == desired.sdk.packages["tool"].source_hash
    assert "clock" in sdk_change.affected
    # agent 只依赖契约，不装工具运行时包
    assert "agent" not in sdk_change.affected


def test_sdk_unchanged_does_not_restart(desired: DesiredState) -> None:
    """SDK 没变就不该因为这条规则重启（否则会陷入永久重启循环）。"""
    result = plan(desired, _actual(desired))
    assert result.counts()["restart"] == 0
    assert all("SDK 变化" not in change.reasons for change in result.services)


def test_missing_sdk_snapshot_counts_as_changed(desired: DesiredState) -> None:
    """老格式快照没有 sdk 段：无法判断，按「变了」处理，宁多重启一次。"""
    snapshot = _actual(desired)
    snapshot.sdk = None
    result = plan(desired, snapshot)
    assert result.counts()["restart"] == len(desired.services)


def test_restart_reasons_detects_artifact_change(desired: DesiredState) -> None:
    """产物指纹变了（依赖锁/解释器）就必须重启，哪怕源码与配置都没动。

    这是兜底判据：``uv.lock`` 或解释器版本变化时，清单/配置/源码/版本/契约/SDK
    六项全都不变，但进程跑的是旧产物——不重启的话新世代声明的 artifact_hash
    与实际运行的永远对不上。
    """
    service = next(iter(desired.services.values()))
    tuned = service.model_copy(update={"artifact_hash": "artifact-new"})
    previous = service.model_copy(update={"artifact_hash": "artifact-old"})

    reasons = restart_reasons(
        current=tuned,
        previous_meta=service.meta,
        previous_config_hash=service.config_hash,
        previous_source_hash=service.source_hash,
        previous_version=service.package_version,
        changed_contracts=set(),
        previous_artifact_hash=previous.artifact_hash,
    )

    assert any("产物变化" in reason for reason in reasons)

    # 老快照没有 artifact_hash（空串）时不做这个判断，避免升级即全量重启
    assert (
        restart_reasons(
            current=tuned,
            previous_meta=service.meta,
            previous_config_hash=service.config_hash,
            previous_source_hash=service.source_hash,
            previous_version=service.package_version,
            changed_contracts=set(),
            previous_artifact_hash="",
        )
        == ()
    )


def test_meta_change_alone_is_detected_from_snapshot(desired: DesiredState) -> None:
    """只改了服务清单（kind / depends_on）也要被预览抓住。

    这些字段不进 config_hash，所以只能靠快照里的 ``meta`` 比出来——reconciler 用
    内存里的 ``DesiredService`` 做同一件事，CLI 只能靠这一列。
    """
    actual = _actual(desired)
    name = "clock"
    service = actual.services[name]
    assert service.meta is not None
    service.meta = service.meta.model_copy(
        update={"depends_on": (*service.meta.depends_on, "session")}
    )

    result = plan(desired, actual)

    change = next(item for item in result.services if item.name == name)
    assert change.action == "restart"
    assert "服务清单变化" in change.reasons
    assert result.note == ""


def test_modern_snapshot_has_no_caveat(desired: DesiredState) -> None:
    """快照里有 meta 时不该再出现"可能多重启几个"的免责声明。"""
    assert plan(desired, _actual(desired)).note == ""


def test_old_snapshot_without_meta_keeps_the_caveat(desired: DesiredState) -> None:
    """旧版本写的快照没有 meta，只能退回"无法判定"的说法。"""
    actual = _actual(desired)
    actual.services["clock"].meta = None

    result = plan(desired, actual)

    assert "meta" in result.note
    assert "apply 一次后即可精确对比" in result.note


def test_missing_state_still_creates_everything(desired: DesiredState) -> None:
    """没有快照时仍是"全部新建"，note 说明原因。"""
    result = plan(desired, None)

    assert result.counts()["create"] == len(desired.services)
    assert "还没部署过" in result.note
