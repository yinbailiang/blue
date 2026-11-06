"""产物（artifact）测试：闭包指纹、组装、共享 base 与回收。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from blue.control.artifacts import (
    COMPLETE_MARKER,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ArtifactRef,
    ArtifactStore,
    artifact_hash,
    editable_installs,
    venv_site_packages,
)
from blue.control.model import (
    ContractMeta,
    DesiredContract,
    DesiredSdk,
    DesiredSdkPackage,
    DesiredService,
    DesiredState,
    SdkMeta,
    ServiceMeta,
    SupervisorSettings,
)
from blue.errors import SupervisorError

PYTHON = "3.14"


def _desired(
    tmp_path: Path,
    *,
    source_hash: str = "src-1",
    sdk_hash: str = "sdk-1",
    contract_hash: str = "contract-1",
    deps_hash: str = "deps-1",
    with_contract: bool = True,
    with_package: bool = False,
) -> DesiredState:
    """构造含一个服务（带契约依赖）的期望状态。"""
    packages: dict[str, DesiredSdkPackage] = {}
    depends_sdk: tuple[str, ...] = ()
    if with_package:
        depends_sdk = ("tool",)
        packages["tool"] = DesiredSdkPackage(
            meta=SdkMeta(name="tool", package="blue-tool-sdk", module="blue_tool_sdk"),
            package_version="0.1.0",
            source_hash="tool-sdk-1",
        )
    service = DesiredService(
        meta=ServiceMeta(
            name="demo",
            kind="tool",
            package="blue-service-demo",
            module="blue_service_demo",
            depends_contracts=("tool",) if with_contract else (),
            depends_sdk=depends_sdk,
        ),
        config={},
        config_hash="cfg-1",
        package_version="0.1.0",
        source_hash=source_hash,
        service_dir=tmp_path / "services" / "demo",
        config_path=tmp_path / "config.json",
    )
    contracts: dict[str, DesiredContract] = {}
    if with_contract:
        contracts["tool"] = DesiredContract(
            meta=ContractMeta(
                name="tool", package="blue-contracts-tool", module="blue_contracts_tool"
            ),
            package_version="0.1.0",
            source_hash=contract_hash,
            contract_dir=tmp_path / "contracts" / "tool",
            contract_path=tmp_path / "contracts" / "tool" / "contract.infd",
        )
    return DesiredState(
        generation="gen-1",
        config_path=tmp_path / "blue.infd",
        base_dir=tmp_path,
        supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
        services={"demo": service},
        contracts=contracts,
        sdk=DesiredSdk(
            package_version="0.1.0", source_hash=sdk_hash, packages=packages
        ),
        bus_url="amqp://guest:guest@127.0.0.1:5672/",
        bus_transport="rabbit",
        deps_hash=deps_hash,
    )


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(
        tmp_path / ".blue",
        project_root=tmp_path,
        python=PYTHON,
        warn_cross_device=False,
    )


def _workspace_venv(tmp_path: Path) -> Path:
    """造一个"开发用 venv"：含第三方包、workspace 包指针与缓存。"""
    venv = tmp_path / ".venv"
    site = venv / "lib" / f"python{PYTHON}" / "site-packages"
    (site / "pydantic").mkdir(parents=True)
    (site / "pydantic" / "__init__.py").write_text("VERSION = '2'\n", encoding="utf-8")
    (site / "typing_extensions.py").write_text("X = 1\n", encoding="utf-8")
    # workspace 包在开发 venv 里是 .pth 指针 + dist-info，绝不能进 base
    (site / "blue.pth").write_text("/somewhere/src\n", encoding="utf-8")
    (site / "blue_service_demo.pth").write_text("/somewhere/src\n", encoding="utf-8")
    (site / "blue-0.1.0.dist-info").mkdir()
    (site / "blue-0.1.0.dist-info" / "METADATA").write_text("", encoding="utf-8")
    (site / "__pycache__").mkdir()
    (site / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    return venv


class _FakeUv:
    """替身：只模拟 uv 建目录/造 wheel，不真的调用外部命令。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, store: ArtifactStore, args: list[str], *, cwd: Path) -> None:
        del store
        self.calls.append(args)
        if len(args) > 1 and args[1] == "venv":
            target = Path(args[2])
            (target / "lib" / f"python{PYTHON}" / "site-packages").mkdir(parents=True)
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("#!/bin/true\n", encoding="utf-8")
        elif len(args) > 1 and args[1] == "build":
            out = Path(args[args.index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            package = args[args.index("--package") + 1].replace("-", "_")
            (out / f"{package}-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")

    def venv_calls(self) -> list[list[str]]:
        return [call for call in self.calls if len(call) > 1 and call[1] == "venv"]

    def install_calls(self) -> list[list[str]]:
        return [call for call in self.calls if len(call) > 1 and call[1] == "pip"]


@pytest.fixture
def fake_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeUv:
    """把外呼换成模拟实现，并准备好"开发用 venv"。"""
    _workspace_venv(tmp_path)
    fake = _FakeUv()

    def run(store: ArtifactStore, args: list[str], *, cwd: Path) -> None:
        fake(store, args, cwd=cwd)

    monkeypatch.setattr(ArtifactStore, "_run", run)
    return fake


def test_artifact_hash_covers_whole_closure(tmp_path: Path) -> None:
    """产物指纹必须对源码、SDK、契约、依赖与解释器都敏感。"""
    base = artifact_hash(
        _desired(tmp_path).services["demo"], _desired(tmp_path), python=PYTHON
    )
    service = _desired(tmp_path).services["demo"]

    cases = {
        "源码": (_desired(tmp_path, source_hash="src-2"), PYTHON),
        "SDK": (_desired(tmp_path, sdk_hash="sdk-2"), PYTHON),
        "契约": (_desired(tmp_path, contract_hash="contract-2"), PYTHON),
        "依赖锁": (_desired(tmp_path, deps_hash="deps-2"), PYTHON),
        "解释器": (_desired(tmp_path), "3.15"),
    }
    for label, (desired, python) in cases.items():
        digest = artifact_hash(desired.services["demo"], desired, python=python)
        assert digest != base, f"{label}变化必须改变产物指纹"

    same = artifact_hash(service, _desired(tmp_path), python=PYTHON)
    assert same == base, "同样的闭包必须得到同样的指纹（内容寻址）"


def test_artifact_hash_ignores_unrelated_contracts(tmp_path: Path) -> None:
    """没在 depends_contracts 里声明的契约，改了也不该重建产物。"""
    desired = _desired(tmp_path, with_contract=False)
    digest = artifact_hash(desired.services["demo"], desired, python=PYTHON)
    other = _desired(tmp_path, with_contract=False, contract_hash="changed")
    assert artifact_hash(other.services["demo"], other, python=PYTHON) == digest


def test_runtime_package_enters_artifact_closure(
    tmp_path: Path, fake_uv: _FakeUv
) -> None:
    """``depends_sdk`` 引用的运行时包要进产物指纹、安装清单与 manifest。"""
    plain = _desired(tmp_path)
    with_package = _desired(tmp_path, with_package=True)
    assert artifact_hash(
        with_package.services["demo"], with_package, python=PYTHON
    ) != artifact_hash(plain.services["demo"], plain, python=PYTHON)

    ref = _store(tmp_path).ensure(with_package.services["demo"], with_package)

    assert ref.manifest()["sdk_packages"] == {
        "tool": {
            "package": "blue-tool-sdk",
            "module": "blue_tool_sdk",
            "package_version": "0.1.0",
            "source_hash": "tool-sdk-1",
        }
    }
    # 写方（_build）和读方（_manifest_hashes）各写一份字段列表，靠这条钉住不漂：
    # 读不懂清单时 wheel 回收会整体跳过，等于 GC 静默失效。
    wheel_keys = _store(tmp_path).keep_wheel_keys({"demo": {ref.hash}})
    assert wheel_keys is not None, "产物清单必须能被 wheel 回收读懂"
    assert "tool-sdk-1" in wheel_keys, "sdk 包的 source_hash 必须进保留集"
    install = fake_uv.install_calls()[0]
    assert "blue-tool-sdk" in install
    built = [
        call[call.index("--package") + 1]
        for call in fake_uv.calls
        if len(call) > 1 and call[1] == "build"
    ]
    assert "blue-tool-sdk" in built


def test_ensure_base_excludes_workspace_and_caches(tmp_path: Path) -> None:
    """共享 base 只放第三方依赖：workspace 指针、dist-info、缓存都不进。"""
    _workspace_venv(tmp_path)
    base_site = _store(tmp_path).ensure_base(_desired(tmp_path))

    names = {entry.name for entry in base_site.iterdir()}
    assert "pydantic" in names
    assert "typing_extensions.py" in names
    assert "blue.pth" not in names
    assert "blue-0.1.0.dist-info" not in names
    assert "__pycache__" not in names
    assert (base_site.parents[2] / COMPLETE_MARKER).is_file()


def test_ensure_base_is_reused_for_same_lock(tmp_path: Path) -> None:
    """依赖锁没变就复用同一份 base（目录 mtime 不变）。"""
    _workspace_venv(tmp_path)
    store = _store(tmp_path)
    first = store.ensure_base(_desired(tmp_path))
    stamp = first.stat().st_mtime_ns

    second = store.ensure_base(_desired(tmp_path))

    assert second == first
    assert second.stat().st_mtime_ns == stamp


def test_ensure_base_requires_workspace_venv(tmp_path: Path) -> None:
    """没有 .venv 时给出可操作的报错，而不是静默降级。"""
    with pytest.raises(SupervisorError, match="uv sync"):
        _store(tmp_path).ensure_base(_desired(tmp_path))


def test_ensure_builds_complete_artifact(tmp_path: Path, fake_uv: _FakeUv) -> None:
    """产物组装：venv + base 指针 + 包安装 + manifest + 完成标记。"""
    desired = _desired(tmp_path)
    store = _store(tmp_path)

    ref = store.ensure(desired.services["demo"], desired)

    assert ref.complete is True
    assert (ref.path / COMPLETE_MARKER).is_file()
    manifest = ref.manifest()
    assert manifest["service"] == "demo"
    assert manifest["artifact_hash"] == ref.hash
    assert manifest["source_hash"] == "src-1"
    assert manifest["deps_hash"] == "deps-1"
    assert manifest["python"] == PYTHON
    assert manifest["contracts"] == {"tool": "contract-1"}
    assert manifest["sdk"] == {"package_version": "0.1.0", "source_hash": "sdk-1"}

    pth = venv_site_packages(ref.venv) / "zz_blue_base.pth"
    assert pth.is_file()
    assert Path(pth.read_text(encoding="utf-8").strip()).is_dir()

    install = fake_uv.install_calls()
    assert len(install) == 1
    assert "--offline" in install[0] and "--no-deps" in install[0]
    assert "blue-service-demo" in install[0]
    assert "blue-contracts-tool" in install[0]
    assert "blue" in install[0]
    # 没有声明的契约不该被装进来
    assert "blue-contracts-session" not in install[0]
    # uv build 收的是**包名**，不是契约名/服务名（这里踩过：--package tool 会失败）
    built = [
        call[call.index("--package") + 1]
        for call in fake_uv.calls
        if len(call) > 1 and call[1] == "build"
    ]
    assert built == ["blue", "blue-contracts-tool", "blue-service-demo"]


def test_ensure_reuses_existing_artifact(tmp_path: Path, fake_uv: _FakeUv) -> None:
    """产物已完整就不再调用 uv（增量构建的关键）。"""
    desired = _desired(tmp_path)
    store = _store(tmp_path)
    store.ensure(desired.services["demo"], desired)
    before = len(fake_uv.calls)

    store.ensure(desired.services["demo"], desired)

    assert len(fake_uv.calls) == before


def test_ensure_skips_services_with_custom_command(
    tmp_path: Path, fake_uv: _FakeUv
) -> None:
    """自定义命令的服务（外部脚本/二进制）不产物化。"""
    desired = _desired(tmp_path)
    service = desired.services["demo"].model_copy(
        update={
            "meta": desired.services["demo"].meta.model_copy(
                update={"command": ("/bin/true",)}
            )
        }
    )

    ref = _store(tmp_path).ensure(service, desired)

    assert ref.complete is False
    assert fake_uv.calls == []


def test_resolve_points_into_artifacts_tree(tmp_path: Path) -> None:
    """resolve 只定位路径，不触发构建。"""
    ref = _store(tmp_path).resolve("demo", "abc123")
    assert ref == ArtifactRef(
        service="demo",
        hash="abc123",
        path=tmp_path / ".blue" / "artifacts" / "demo" / "abc123",
    )
    assert ref.complete is False


def test_gc_removes_unreferenced_artifacts(tmp_path: Path) -> None:
    """只删除没有被保留世代引用的产物。"""
    store = _store(tmp_path)
    root = store.root / "demo"
    for digest in ("keep-me", "drop-me"):
        (root / digest / "venv").mkdir(parents=True)
        (root / digest / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")
    (store.base_root / "deps-1").mkdir(parents=True)
    (store.base_root / "deps-old").mkdir(parents=True)

    removed = store.gc({"demo": {"keep-me"}})
    removed_bases = store.gc_bases({"deps-1"})

    assert removed == ["demo/drop-me"]
    assert (root / "keep-me").is_dir()
    assert removed_bases == ["_base/deps-old"]


def test_gc_skips_staging_directories(tmp_path: Path) -> None:
    """构建中的 ``<指纹>.tmp-<pid>`` 一律不动——删了就是把别人的 apply 打崩。"""
    store = _store(tmp_path)
    (store.root / "demo" / "drop-me").mkdir(parents=True)
    staging = store.root / "demo" / "building.tmp-424242"
    staging.mkdir()
    (store.base_root / "deps-old").mkdir(parents=True)
    base_staging = store.base_root / "deps-2.tmp-424242"
    base_staging.mkdir(parents=True)

    removed = store.gc({})
    removed_bases = store.gc_bases(set())

    assert removed == ["demo/drop-me"]
    assert staging.is_dir()
    assert removed_bases == ["_base/deps-old"]
    assert base_staging.is_dir()


def test_gc_dry_run_keeps_everything(tmp_path: Path) -> None:
    """dry-run 只报告。"""
    store = _store(tmp_path)
    (store.root / "demo" / "dead" / "venv").mkdir(parents=True)
    (store.base_root / "old").mkdir(parents=True)

    removed = store.gc({}, dry_run=True)
    removed_bases = store.gc_bases(set(), dry_run=True)

    assert removed == ["demo/dead"]
    assert removed_bases == ["_base/old"]
    assert (store.root / "demo" / "dead").is_dir()


def test_link_tree_reuses_inodes_on_same_filesystem(tmp_path: Path) -> None:
    """同分区硬链接：两份产物共享 inode，磁盘不重复占用。"""
    from blue.control.artifacts import link_tree

    source = tmp_path / "src" / "file.txt"
    source.parent.mkdir()
    source.write_text("hello\n", encoding="utf-8")
    first = tmp_path / "a" / "file.txt"
    second = tmp_path / "b" / "file.txt"
    link_tree(source, first)
    link_tree(source, second)

    assert first.read_text(encoding="utf-8") == "hello\n"
    if os.stat(source).st_dev == os.stat(first).st_dev:
        assert os.stat(first).st_ino == os.stat(second).st_ino


def test_manifest_missing_returns_empty(tmp_path: Path) -> None:
    """manifest 缺失或损坏时返回空 dict，不抛异常。"""
    ref = _store(tmp_path).resolve("demo", "nope")
    (ref.path).mkdir(parents=True)
    assert ref.manifest() == {}
    (ref.path / MANIFEST_NAME).write_text("{bad", encoding="utf-8")
    assert ref.manifest() == {}


def test_venv_site_packages_rejects_broken_layout(tmp_path: Path) -> None:
    """结构异常的 venv 要立刻报错。"""
    with pytest.raises(SupervisorError, match="缺少 lib"):
        venv_site_packages(tmp_path / "nope")


def test_manifest_is_json_readable(tmp_path: Path, fake_uv: _FakeUv) -> None:
    """manifest 落盘为合法 JSON（排障时要能直接 cat）。"""
    desired = _desired(tmp_path)
    ref = _store(tmp_path).ensure(desired.services["demo"], desired)
    payload = json.loads((ref.path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["module"] == "blue_service_demo"
    assert sorted(payload) == sorted(
        [
            "version",
            "service",
            "artifact_hash",
            "package",
            "module",
            "package_version",
            "source_hash",
            "sdk",
            "sdk_packages",
            "contracts",
            "deps_hash",
            "python",
            "base",
            "interpreter",
        ]
    )


def _mark_editable(site: Path, name: str, target: Path) -> None:
    """写一份 PEP 610 的 editable 记录（真实 uv/pip 装 editable 时就是这么落盘的）。"""
    dist_info = site / f"{name}-6.2.0.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": target.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )


def test_editable_install_outside_workspace_is_detected(tmp_path: Path) -> None:
    """工作区外的 editable 依赖必须被扫出来——它的内容没法固化进产物。"""
    venv = _workspace_venv(tmp_path)
    site = venv_site_packages(venv)
    upstream = tmp_path.parent / "upstream_src"
    upstream.mkdir(exist_ok=True)
    _mark_editable(site, "infinity_bus", upstream)

    found = editable_installs(site, project_root=tmp_path)

    assert found == [("infinity_bus", upstream)]


def test_editable_install_inside_workspace_is_allowed(tmp_path: Path) -> None:
    """工作区成员也是 editable 安装，但它们以 wheel 装进产物，不该拦。"""
    venv = _workspace_venv(tmp_path)
    site = venv_site_packages(venv)
    _mark_editable(site, "blue_tool_sdk", tmp_path / "sdk" / "tool")

    assert editable_installs(site, project_root=tmp_path) == []


def test_egg_link_counts_as_editable(tmp_path: Path) -> None:
    """老式 setuptools 的 ``.egg-link`` 同样是 editable 标记，不能漏。"""
    venv = _workspace_venv(tmp_path)
    site = venv_site_packages(venv)
    upstream = tmp_path.parent / "legacy_src"
    upstream.mkdir(exist_ok=True)
    (site / "legacy.egg-link").write_text(f"{upstream}\n.\n", encoding="utf-8")

    assert editable_installs(site, project_root=tmp_path) == [("legacy", upstream)]


def test_reject_editable_installs_names_the_package_and_the_fix(tmp_path: Path) -> None:
    """报错要能直接照着做：包名、路径、两条处理办法都要在里面。"""
    venv = _workspace_venv(tmp_path)
    site = venv_site_packages(venv)
    upstream = tmp_path.parent / "upstream_src"
    upstream.mkdir(exist_ok=True)
    _mark_editable(site, "infinity_bus", upstream)

    with pytest.raises(SupervisorError) as caught:
        _store(tmp_path).reject_editable_installs()

    message = str(caught.value)
    assert "infinity_bus" in message
    assert str(upstream) in message
    assert "uv sync" in message and "uv build" in message


def test_ensure_base_rejects_editable_install(tmp_path: Path) -> None:
    """克隆 base 之前就失败——不给"少一个模块的 base"落盘的机会。"""
    venv = _workspace_venv(tmp_path)
    site = venv_site_packages(venv)
    upstream = tmp_path.parent / "upstream_src"
    upstream.mkdir(exist_ok=True)
    _mark_editable(site, "infinity_bus", upstream)

    store = _store(tmp_path)
    with pytest.raises(SupervisorError, match="editable 安装"):
        store.ensure_base(_desired(tmp_path))
    # 失败得干净：base 目录一个都不该留下
    assert not list((tmp_path / ".blue" / "artifacts" / "_base").glob("*"))


def test_reject_editable_installs_tolerates_missing_venv(tmp_path: Path) -> None:
    """没有开发用 .venv 时不该在这里报错（该由 _workspace_venv 说出更准的话）。"""
    _store(tmp_path).reject_editable_installs()


def _fake_artifact(
    root: Path, service: str, digest: str, manifest: dict[str, object]
) -> None:
    """造一个"已完成的产物"（只需 manifest，gc_wheels 只读它）。"""
    path = root / "artifacts" / service / digest
    path.mkdir(parents=True, exist_ok=True)
    (path / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def _manifest(**fields: object) -> dict[str, object]:
    """按 ``_build`` 的真实形态造一份 v1 清单：``sdk_packages`` 的值是对象。"""
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
        "service": "demo",
        "artifact_hash": "art-1",
        "package": "blue-service-demo",
        "module": "blue_service_demo",
        "package_version": "0.1.0",
        "source_hash": "svc-src",
        "sdk": {"package_version": "0.1.0", "source_hash": "blue-src"},
        "sdk_packages": {},
        "contracts": {},
        "deps_hash": "deps-1",
        "python": PYTHON,
        "base": "/tmp/base",
        "interpreter": "/tmp/python",
    }
    manifest.update(fields)
    return manifest


def _fake_wheel(root: Path, key: str) -> None:
    directory = root / "wheels" / key
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")


def test_keep_wheel_keys_covers_every_package_hash(tmp_path: Path) -> None:
    """wheel 目录按每个包各自的源码指纹命名，服务自己的 hash 不够。"""
    root = tmp_path / ".blue"
    _fake_artifact(
        root,
        "demo",
        "art-1",
        _manifest(
            sdk_packages={
                "tool": {
                    "package": "blue-tool-sdk",
                    "module": "blue_tool_sdk",
                    "package_version": "0.1.0",
                    "source_hash": "sdk-tool-src",
                }
            },
            contracts={"tool": "contract-src"},
        ),
    )
    store = _store(tmp_path)

    assert store.keep_wheel_keys({"demo": {"art-1"}}) == {
        "unknown",
        "svc-src",
        "blue-src",
        "sdk-tool-src",
        "contract-src",
    }


def test_gc_wheels_removes_only_unreferenced(tmp_path: Path) -> None:
    """没人引用的 wheel 目录才删；被保留产物引用的必须留着。"""
    root = tmp_path / ".blue"
    _fake_artifact(root, "demo", "art-1", _manifest())
    for key in ("svc-src", "blue-src", "stale-src"):
        _fake_wheel(root, key)
    store = _store(tmp_path)

    removed = store.gc_wheels(store.keep_wheel_keys({"demo": {"art-1"}}))

    assert removed == ["stale-src"]
    assert not (root / "wheels" / "stale-src").exists()
    assert (root / "wheels" / "svc-src").is_dir()
    assert (root / "wheels" / "blue-src").is_dir()


def test_gc_wheels_dry_run_keeps_everything(tmp_path: Path) -> None:
    """dry-run 只报告，不落刀。"""
    root = tmp_path / ".blue"
    _fake_wheel(root, "stale-src")
    store = _store(tmp_path)

    assert store.gc_wheels({"svc-src"}, dry_run=True) == ["stale-src"]
    assert (root / "wheels" / "stale-src").is_dir()


def test_gc_wheels_bails_out_when_manifest_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """清单读不出来就一个都不删——宁可留垃圾，也不要删掉活产物引用的 wheel。"""
    root = tmp_path / ".blue"
    _fake_wheel(root, "svc-src")
    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING):
        keys = store.keep_wheel_keys({"demo": {"missing-artifact"}})

    assert keys is None
    assert "读不出产物清单" in caplog.text
    assert store.gc_wheels(keys) == []
    assert (root / "wheels" / "svc-src").is_dir()


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"version": 2}, id="版本不认识"),
        pytest.param({"source_hash": 1}, id="source_hash 不是字符串"),
        pytest.param({"sdk": {"package_version": "0.1.0"}}, id="sdk 缺 source_hash"),
        pytest.param(
            {"sdk_packages": {"tool": "sdk-tool-src"}}, id="sdk_packages 值是字符串"
        ),
        pytest.param(
            {"sdk_packages": {"tool": {"package": "blue-tool-sdk"}}},
            id="sdk_packages 条目缺 source_hash",
        ),
        pytest.param(
            {"contracts": {"tool": {"source_hash": "contract-src"}}},
            id="contracts 值是对象",
        ),
    ],
)
def test_keep_wheel_keys_bails_out_on_unknown_manifest_shape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fields: dict[str, object]
) -> None:
    """认不出的结构不许猜：告警 + 一个都不删（删错就是删掉还在用的 wheel）。"""
    root = tmp_path / ".blue"
    _fake_artifact(root, "demo", "art-1", _manifest(**fields))
    _fake_wheel(root, "svc-src")
    _fake_wheel(root, "stale-src")
    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING):
        keys = store.keep_wheel_keys({"demo": {"art-1"}})

    assert keys is None
    assert "跳过 wheel 回收判定" in caplog.text
    assert store.gc_wheels(keys) == []
    assert (root / "wheels" / "svc-src").is_dir()
    assert (root / "wheels" / "stale-src").is_dir()
