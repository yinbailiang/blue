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
    ActivationSettings,
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


def test_legacy_generation_file_stays_readable(tmp_path: Path) -> None:
    """旧世代记录里的**布尔** supervisor 开关仍然读得懂（回归用例）。

    配置分组化把 ``idempotency`` / ``journal`` 从布尔改成了对象，而
    ``generation.json`` 是**跟过去签的合同**：老记录里存的是
    ``"journal": true``。少了这层兼容，校验会失败、``list()`` 对坏目录是静默
    跳过的——实测 11-07 那批世代（含当时正在跑的 0046）整体从历史里消失，
    回滚目标一下退化到好几天前，而且没有任何一条日志说"你的历史没了"。
    """
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    path = generation.path / GENERATION_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["supervisor"] = {
        "state_dir": str(tmp_path / ".blue"),
        "uv_frozen": True,
        "log_max_bytes": 1048576,
        "log_backups": 1,
        "restart_backoff": 0.5,
        "restart_backoff_max": 30.0,
        "restart_max_attempts": 10,
        "stable_after": 60.0,
        "registry_ttl": 15.0,
        "registry_sweep_interval": 2.0,
        "idempotency": True,
        "idempotency_retention": 86400.0,
        "idempotency_sweep_interval": 600.0,
        "journal": True,
        "journal_retention": 86400.0,
        "journal_max_rows": None,
        "journal_max_bytes": 67108864,
        "journal_payload_mode": "digest",
        "journal_payload_allowlist": [],
        "journal_payload_max_bytes": 8192,
        "journal_sweep_interval": 600.0,
        "activation_timeout": 120.0,
        "generation_keep": 5,
        "generation_auto_rollback": True,
        "artifact_python": "3.14",
        "artifact_link_mode": "hardlink",
        "artifact_warn_cross_device": True,
        "await_control_change": True,
        "smoke_enabled": True,
        "smoke_timeout": 20.0,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    reloaded = store.load(generation.id)

    assert [item.id for item in store.list()] == [generation.id]
    assert reloaded.data.supervisor is not None
    # 布尔被升级成分组对象，取值不变
    assert reloaded.data.supervisor.journal.enabled is True
    assert reloaded.data.supervisor.idempotency.enabled is True
    # 旧记录里没有的字段取默认，不会因为缺组就整条记录作废
    assert reloaded.data.supervisor.activation.timeout > 0


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
                state_dir=tmp_path / ".blue",
                activation=ActivationSettings(timeout=12.0),
            )
        }
    )
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")

    stored = json.loads((generation.path / GENERATION_FILE).read_text(encoding="utf-8"))
    assert stored["supervisor"]["activation"]["timeout"] == 12.0

    rebuilt = store.load(generation.id).to_desired(
        supervisor=SupervisorSettings(
            state_dir=tmp_path / ".blue", activation=ActivationSettings(timeout=999.0)
        ),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
    )
    assert rebuilt.supervisor.activation.timeout == 12.0, "世代里的设置优先"


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
            state_dir=tmp_path / ".blue", activation=ActivationSettings(timeout=33.0)
        ),
        base_dir=tmp_path,
        artifacts=_artifacts(tmp_path),
    )

    assert rebuilt.supervisor.activation.timeout == 33.0


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


def test_gc_protects_generations_in_use(tmp_path: Path) -> None:
    """运行中进程仍在用的世代必须保留。

    只重启变了服务 → 进程跨世代共存，老进程的 BLUE_CONFIG_FILE 还指向它启动时
    那一代。删掉那一代，服务只是靠内存缓存还活着，一旦重读配置就崩（实测踩过）。
    """
    store = _store(tmp_path)
    for index in range(1, 5):
        generation = store.create(
            _desired(tmp_path, generation=f"gen-{index}"),
            _artifacts(tmp_path),
            reason="apply",
        )
        store.mark_stable(generation)
        store.activate(generation)
    head = store.create(
        _desired(tmp_path, generation="gen-5"), _artifacts(tmp_path), reason="apply"
    )
    store.activate(head)

    # 不传 in_use 时 1、2 都会被删（既不在最近 1 个里，也不是最近两个 stable）
    assert store.gc(keep=1, dry_run=True) == [1, 2]

    removed = store.gc(keep=1, in_use={2})

    assert 2 not in removed, "在用的世代不能被回收"
    assert removed == [1]
    assert store.load(2).complete is True


def test_mark_stable_keeps_tool_observation(tmp_path: Path) -> None:
    """`.stable` 有两个写者：CLI 冒烟（带观测值）与调谐器闸门（不带）。

    后写的不许抹掉观测值——实测踩过：CLI 写完 {"tools": 20}，闸门重标一次变成
    "ok"，工具登记数就丢了。
    """
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )

    store.mark_stable(generation, tools=20)
    store.mark_stable(generation)  # 调谐器闸门通过，不带观测值

    marker = (generation.path / ".stable").read_text(encoding="utf-8")
    assert '"tools": 20' in marker
    assert generation.state == "stable"


def test_mark_failed_clears_stable_marker(tmp_path: Path) -> None:
    """冒烟失败后必须从 stable 变成 failed（回滚的可观测依据）。"""
    store = _store(tmp_path)
    generation = store.create(
        _desired(tmp_path, generation="gen-1"), _artifacts(tmp_path), reason="apply"
    )
    store.mark_stable(generation, tools=20)

    store.mark_failed(generation, "冒烟失败：缺少工具")

    assert generation.state == "failed"
    assert "冒烟失败" in (generation.path / ".failed").read_text(encoding="utf-8")


def test_create_dedupes_against_unactivated_generation(tmp_path: Path) -> None:
    """``apply --staged`` 之后紧跟 ``apply``：内容相同就不该再造一个世代。

    只和 current 比是不够的——staged 世代不是 current，会凭空多一代逐字节相同的
    目录（实测踩过：0042/0043）。
    """
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    staged = store.create(desired, _artifacts(tmp_path), reason="apply")
    assert store.current() is None

    again = store.create(desired, _artifacts(tmp_path), reason="apply")

    assert again.id == staged.id
    assert len(store.list()) == 1


def test_create_reuses_failed_generation(tmp_path: Path) -> None:
    """上一代激活失败、内容又没变时复用它：同输入同结果，再造一份相同目录没意义。

    复用时**不**抹掉 ``.failed``——失败的证据留着，下一次激活会重新记一遍。
    """
    store = _store(tmp_path)
    desired = _desired(tmp_path, generation="gen-1")
    generation = store.create(desired, _artifacts(tmp_path), reason="apply")
    store.mark_failed(generation, "冒烟失败")

    again = store.create(desired, _artifacts(tmp_path), reason="apply")

    assert again.id == generation.id
    assert len(store.list()) == 1
    assert again.failed is True


def _point_current(store: GenerationStore, *, ident: int, fingerprint: str) -> None:
    """造一个 ``current`` 指向的世代（只写 ``current()`` 需要的字段）。"""
    path = store.root / f"{ident:04d}"
    path.mkdir(parents=True, exist_ok=True)
    (path / GENERATION_FILE).write_text(
        json.dumps({"id": ident, "generation": fingerprint, "created_at": 0.0}),
        encoding="utf-8",
    )
    (path / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")
    link = store.current_link
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path("generations") / f"{ident:04d}")


def test_assert_current_accepts_matching_prefix(tmp_path: Path) -> None:
    """--expect 支持只写前缀（和 git 一样），匹配就放行。"""
    store = _store(tmp_path)
    _point_current(store, ident=7, fingerprint="d2b32fecd171ffff")

    assert store.assert_current("d2b32fec").id == 7
    assert store.assert_current("d2b32fecd171ffff").id == 7


def test_assert_current_refuses_when_world_moved(tmp_path: Path) -> None:
    """预览之后 current 变了 → 直接拒绝，并说清实际是什么。"""
    store = _store(tmp_path)
    _point_current(store, ident=7, fingerprint="aaaaaaaaaaaa")

    with pytest.raises(BlueError) as caught:
        store.assert_current("bbbbbbbb")

    message = str(caught.value)
    assert "当前世代已变化" in message
    assert "aaaaaaaaaaaa" in message
    assert "0007" in message
    assert "blue plan" in message


def test_assert_current_requires_existing_generation(tmp_path: Path) -> None:
    """没有 current 时无从校验，要说人话而不是崩。"""
    with pytest.raises(BlueError, match="还没有当前世代"):
        _store(tmp_path).assert_current("abc")


def test_assert_current_rejects_empty_value(tmp_path: Path) -> None:
    """空串当成用户写错了，不要静默放过。"""
    store = _store(tmp_path)
    _point_current(store, ident=7, fingerprint="aaaaaaaaaaaa")

    with pytest.raises(BlueError, match="不能为空"):
        store.assert_current("   ")
