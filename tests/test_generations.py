"""世代（generations）测试：冻结、原子翻转、崩溃回退、单服务回滚与回收。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blue.control.artifacts import ArtifactStore
from blue.control.generations import (
    COMPLETE_MARKER,
    FAILED_MARKER,
    GENERATION_FILE,
    STABLE_MARKER,
    GenerationStore,
)
from blue.control.model import (
    DesiredService,
    DesiredState,
    ServiceMeta,
    SupervisorSettings,
)
from blue.errors import BlueError


def _desired(
    tmp_path: Path,
    *,
    generation: str,
    services: dict[str, str] | None = None,
) -> DesiredState:
    """构造最小期望状态；服务用自定义 command，因此不需要产物。"""
    specs = services or {"demo": "hash-demo-1"}
    desired_services: dict[str, DesiredService] = {}
    for name, config_hash in specs.items():
        desired_services[name] = DesiredService(
            meta=ServiceMeta(
                name=name,
                kind="tool",
                package=f"pkg-{name}",
                module=name,
                command=("/bin/true",),
            ),
            config={"value": config_hash},
            config_hash=config_hash,
            service_dir=tmp_path / "services" / name,
            config_path=tmp_path / "unused" / f"{name}.json",
        )
    return DesiredState(
        generation=generation,
        config_path=tmp_path / "blue.infd",
        base_dir=tmp_path,
        supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
        services=desired_services,
        bus_url="amqp://guest:guest@127.0.0.1:5672/",
        bus_transport="rabbit",
        deps_hash="deps-1",
    )


def _store(tmp_path: Path) -> GenerationStore:
    return GenerationStore(tmp_path / ".blue")


def _artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / ".blue", project_root=tmp_path)


def test_create_writes_complete_generation(tmp_path: Path) -> None:
    """新世代目录包含元数据、脱敏快照、服务配置与完成标记。"""
    desired = _desired(tmp_path, generation="gen-1")
    generation = _store(tmp_path).create(desired, _artifacts(tmp_path), reason="apply")

    assert generation.id == 1
    assert generation.parent_id is None
    assert generation.generation == "gen-1"
    assert generation.complete is True
    assert generation.stable is False
    assert generation.state == "pending"
    assert (generation.path / GENERATION_FILE).is_file()
    assert (generation.path / COMPLETE_MARKER).is_file()
    assert (generation.path / "desired.json").is_file()

    config_path = generation.config_path("demo")
    assert config_path.is_file()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["config"] == {"value": "hash-demo-1"}
    assert payload["infra"]["bus_url"] == "amqp://guest:guest@127.0.0.1:5672/"
    # 世代目录里有明文密钥，权限必须是 0600
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_create_dedupes_identical_desired_state(tmp_path: Path) -> None:
    """内容相同且当前世代已 stable 时，不产生新世代（对应 Nix 的 store path 去重）。"""
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    first = store.create(desired, _artifacts(tmp_path), reason="apply")
    store.mark_stable(first)
    store.activate(first)

    again = store.create(desired, _artifacts(tmp_path), reason="apply")

    assert again.id == first.id
    assert len(store.list()) == 1


def test_create_increments_id_and_parent(tmp_path: Path) -> None:
    """每次内容变化产生新世代，并记住父世代。"""
    store = _store(tmp_path)
    first = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    second = store.create(
        _desired(tmp_path, generation="gen-2"), _artifacts(tmp_path), reason="apply"
    )

    assert (first.id, second.id) == (1, 2)
    assert second.parent_id == first.id
    assert second.path.name == "0002"


def test_activate_flips_current_atomically(tmp_path: Path) -> None:
    """current 是指向世代目录的相对符号链接，切换后立刻生效。"""
    store = _store(tmp_path)
    first = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    second = store.create(
        _desired(tmp_path, generation="gen-2"), _artifacts(tmp_path), reason="apply"
    )

    store.activate(first)
    active = store.current()
    assert active is not None
    assert active.id == first.id
    assert store.current_link.is_symlink()
    assert os.readlink(store.current_link) == "generations/0001"

    store.activate(second)
    active = store.current()
    assert active is not None
    assert active.id == second.id
    # 临时链接不能残留
    assert not list(tmp_path.glob(".blue/current.tmp-*"))


def test_activate_rejects_incomplete_generation(tmp_path: Path) -> None:
    """没有 .complete 的目录是半成品，禁止激活。"""
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    (generation.path / COMPLETE_MARKER).unlink()

    with pytest.raises(BlueError, match="不完整"):
        store.activate(store.load(generation.id))


def test_current_is_none_without_or_with_broken_link(tmp_path: Path) -> None:
    """没有 current、或指向已删除目录，都返回 None（不能崩）。"""
    store = _store(tmp_path)
    assert store.current() is None

    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    store.activate(generation)
    import shutil

    shutil.rmtree(generation.path)
    assert store.current() is None


def test_mark_stable_and_failed(tmp_path: Path) -> None:
    """stable / failed 互斥：标记另一个会清掉前一个。"""
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )

    store.mark_stable(generation)
    assert (generation.path / STABLE_MARKER).is_file()
    assert store.load(generation.id).state == "stable"

    store.mark_failed(generation, "boom")
    reloaded = store.load(generation.id)
    assert reloaded.state == "failed"
    assert not (generation.path / STABLE_MARKER).exists()
    assert "boom" in (generation.path / FAILED_MARKER).read_text(encoding="utf-8")


def test_latest_stable_skips_newest_pending(tmp_path: Path) -> None:
    """回滚目标取"最新的 stable"，跳过未验证的世代。"""
    store = _store(tmp_path)
    first = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    store.mark_stable(first)
    second = store.create(
        _desired(tmp_path, generation="gen-2"), _artifacts(tmp_path), reason="apply"
    )

    assert store.latest_stable() is not None
    assert store.latest_stable().id == first.id  # type: ignore[union-attr]
    assert store.latest_stable(exclude=first.id) is None
    assert second.stable is False


def test_to_desired_rebuilds_state_from_generation(tmp_path: Path) -> None:
    """世代是自足的：不编译 blue.infd 也能重建期望状态。"""
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")
    store.mark_stable(generation)
    store.activate(generation)

    rebuilt = store.load(generation.id).to_desired(
        supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
    )

    assert rebuilt.generation == "gen-1"
    assert rebuilt.deps_hash == "deps-1"
    assert rebuilt.bus_url == desired.bus_url
    service = rebuilt.services["demo"]
    assert service.config == {"value": "hash-demo-1"}
    assert service.cwd == tmp_path
    assert service.config_path == generation.config_path("demo")
    assert service.artifact_hash == ""  # 自定义命令的服务不产物化
    assert service.artifact_python is None


def test_to_desired_rejects_missing_artifact(tmp_path: Path) -> None:
    """引用的产物被删掉时必须报错，而不是悄悄退回工作区代码。"""
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")
    data = generation.data.model_copy(
        update={
            "services": {
                "demo": generation.data.services["demo"].model_copy(
                    update={"artifact_hash": "deadbeef"}
                )
            }
        }
    )
    broken = type(generation)(path=generation.path, data=data)

    with pytest.raises(BlueError, match="产物已不存在"):
        broken.to_desired(
            supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
            base_dir=tmp_path,
            artifacts=_artifacts(tmp_path),
        )
    # strict=False 时退回 uv run（供启动期兜底）
    relaxed = broken.to_desired(
        supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
        strict=False,
    )
    assert relaxed.services["demo"].artifact_python is None


def test_rebind_service_creates_linear_new_generation(tmp_path: Path) -> None:
    """单服务回滚：新世代 = 当前世代，但把目标服务换成旧世代那份。"""
    store = _store(tmp_path)
    old = store.create(
        _desired(tmp_path, generation="gen-1", services={"demo": "h1", "other": "o1"}),
        _artifacts(tmp_path),
        reason="apply",
    )
    store.mark_stable(old)
    current = store.create(
        _desired(tmp_path, generation="gen-2", services={"demo": "h2", "other": "o2"}),
        _artifacts(tmp_path),
        reason="apply",
    )
    store.mark_stable(current)
    store.activate(current)

    rebound = store.rebind_service(service="demo", source=store.load(old.id))

    assert rebound.id == current.id + 1
    assert rebound.parent_id == current.id
    assert "rollback demo" in rebound.data.reason
    # demo 回到旧世代，other 保持当前世代
    assert rebound.data.services["demo"].config_hash == "h1"
    assert rebound.data.services["other"].config_hash == "o2"
    assert bypassed(rebound.config_path("demo"), "h1")
    assert bypassed(rebound.config_path("other"), "o2")
    # 合成指纹与任何 blue.infd 编译结果都不同，下次 apply 会恢复配置描述的样子
    assert rebound.generation not in {"gen-1", "gen-2"}


def bypassed(config_path: Path, config_hash: str) -> bool:
    """世代里的服务配置是否对应某个 config 值。"""
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return payload["config"].get("value") == config_hash


def test_rebind_service_requires_current_generation(tmp_path: Path) -> None:
    """没有当前世代时单服务回滚直接报错。"""
    store = _store(tmp_path)
    source = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    with pytest.raises(BlueError, match="没有当前世代"):
        store.rebind_service(service="demo", source=source)


def test_rebind_service_rejects_unknown_service(tmp_path: Path) -> None:
    """目标服务必须存在于两个世代里。"""
    store = _store(tmp_path)
    source = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    store.mark_stable(source)
    store.activate(source)
    with pytest.raises(BlueError, match="没有服务"):
        store.rebind_service(service="ghost", source=source)


def test_gc_keeps_recent_current_and_newest_stable(tmp_path: Path) -> None:
    """gc 保留最近 N 个 + 当前 + 最新 stable，其余删除。"""
    store = _store(tmp_path)
    for index in range(1, 6):
        generation = store.create(
            _desired(tmp_path, generation=f"gen-{index}"),
            _artifacts(tmp_path),
            reason="apply",
        )
        store.mark_stable(generation)

    removed = store.gc(keep=2)

    assert removed == [1, 2, 3]
    assert [generation.id for generation in store.list()] == [4, 5]


def test_gc_never_removes_current(tmp_path: Path) -> None:
    """当前世代即使不在最近 N 个里也必须保留。"""
    store = _store(tmp_path)
    first = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    store.mark_stable(first)
    store.activate(first)
    for index in range(2, 5):
        store.create(
            _desired(tmp_path, generation=f"gen-{index}"),
            _artifacts(tmp_path),
            reason="apply",
        )

    removed = store.gc(keep=1)

    assert first.id not in removed
    assert store.current() is not None
    assert store.current().id == first.id  # type: ignore[union-attr]


def test_keep_hashes_collects_only_surviving_generations(tmp_path: Path) -> None:
    """gc 后仍被引用的产物指纹，要覆盖所有保留世代。"""
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    first = store.create(desired, _artifacts(tmp_path), reason="apply")
    store.mark_stable(first)
    store.activate(first)
    store.create(
        _desired(tmp_path, generation="gen-2"), _artifacts(tmp_path), reason="apply"
    )

    keep = store.keep_hashes(keep=1)

    # 自定义命令的服务没有产物，因此不该出现在保留集合里
    assert keep == {}


def test_corrupt_generation_is_skipped(tmp_path: Path) -> None:
    """元数据损坏的目录不参与列表（不能把整个系统带崩）。"""
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    (generation.path / GENERATION_FILE).write_text("{not json", encoding="utf-8")

    assert store.list() == []


def test_gc_dry_run_touches_nothing(tmp_path: Path) -> None:
    """--dry-run 只报告，不删除。"""
    store = _store(tmp_path)
    for index in range(1, 4):
        generation = store.create(
            _desired(tmp_path, generation=f"gen-{index}"),
            _artifacts(tmp_path),
            reason="apply",
        )
        store.mark_stable(generation)

    removed = store.gc(keep=1, dry_run=True)

    # 三个都 stable：保最近 1 个 + 当前 + 最近两个 stable → 只回收最老的
    assert removed == [1]
    assert len(store.list()) == 3


def test_generation_freezes_supervisor_settings(tmp_path: Path) -> None:
    """控制面设置必须随世代冻结。

    否则 ``apply --config other.infd`` 改了 activation_timeout 也不生效：
    调谐器仍用自己那个 config_path 编译出来的旧值（真机上就是这么翻的车）。
    """
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1").model_copy(
        update={
            "supervisor": SupervisorSettings(
                state_dir=tmp_path / ".blue", activation_timeout=12.0
            )
        }
    )
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")

    stored = json.loads((generation.path / GENERATION_FILE).read_text(encoding="utf-8"))
    assert stored["supervisor"]["activation_timeout"] == 12.0

    rebuilt = store.load(generation.id).to_desired(
        supervisor=SupervisorSettings(
            state_dir=tmp_path / ".blue", activation_timeout=999.0
        ),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
    )
    assert rebuilt.supervisor.activation_timeout == 12.0, "世代里的设置优先"


def test_to_desired_falls_back_to_bootstrap_settings(tmp_path: Path) -> None:
    """老世代没存 supervisor 设置时，退回调用方给的 bootstrap 设置。"""
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")
    legacy = type(generation)(
        path=generation.path,
        data=generation.data.model_copy(update={"supervisor": None}),
    )

    rebuilt = legacy.to_desired(
        supervisor=SupervisorSettings(
            state_dir=tmp_path / ".blue", activation_timeout=33.0
        ),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
    )

    assert rebuilt.supervisor.activation_timeout == 33.0


def test_gc_keeps_two_stable_generations(tmp_path: Path) -> None:
    """gc 必须留住两个 stable：否则自动回滚没有目标。"""
    store = _store(tmp_path)
    for index in (1, 2, 3):
        generation = store.create(
            _desired(tmp_path, generation=f"gen-{index}"),
            _artifacts(tmp_path),
            reason="apply",
        )
        store.mark_stable(generation)
        store.activate(generation)
    head = store.create(
        _desired(tmp_path, generation="gen-4"), _artifacts(tmp_path), reason="apply"
    )
    store.activate(head)

    removed = store.gc(keep=1)

    assert removed == [1]
    rollback_target = store.latest_stable(exclude=head.id)
    assert rollback_target is not None, "gc 之后仍要能回滚"
    assert rollback_target.id == 3
