"""服务产物（artifact）：内容寻址的代码快照 + 独立可迁移 venv。

每个产物由"产物闭包"指纹命名，落在 ``.blue/artifacts/<service>/<hash>/``：

- ``venv/``：``uv venv --relocatable``，第三方依赖通过 ``.pth`` 指向共享 base，
  workspace 包从本地 wheel 离线安装；
- ``manifest.json``：组装成分（源码/版本/SDK/契约/锁/解释器），便于审计与排障；
- ``.complete``：写完才落——缺它说明是半成品，绝不允许激活。

租约：产物一旦 ``.complete`` 就不再改动。想"改代码"只能产出新指纹，于是回滚
永远是"换指针"，而不是"覆盖回去"。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from blue.control.model import DesiredService, DesiredState
from blue.errors import SupervisorError

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "artifact_hash",
    "editable_installs",
    "link_tree",
    "venv_site_packages",
]

logger = logging.getLogger(__name__)

COMPLETE_MARKER = ".complete"
"""产物目录写完（且可启动）的标记。"""

MANIFEST_NAME = "manifest.json"
"""产物组装成分清单。"""

MANIFEST_VERSION = 1
"""清单格式版本；读写两侧共用，认不出的版本一律不猜。"""

BASE_MARKER = ".complete"
"""共享依赖 base 的完成标记。"""

_SKIP_SITE_PACKAGES = frozenset({"pip", "setuptools", "wheel", "__pycache__"})
"""这些不该进共享 base：产物用 ``python -m`` 启动，不需要打包工具。"""

_SKIP_PREFIXES = ("blue", "_virtualenv")

"""workspace 包在开发用 venv 里是 ``*.pth`` 指针 + dist-info，绝不能进 base。

它们必须以 wheel 形式装进每个产物，否则产物会"借用"工作区源码——那正是我们要
消灭的可变依赖。
"""

_DIRECT_URL_RECORD = "direct_url.json"
"""PEP 610 的安装来源记录；``dir_info.editable`` 是"这是 editable 安装"的权威判据。"""

_STAGING_MARK = ".tmp-"
"""构建中的临时目录标记：``<指纹>.tmp-<pid>`` / ``<锁指纹>.tmp-<pid>``。"""


@dataclass(frozen=True)
class ArtifactRef:
    """一个已构建产物的引用。"""

    service: str
    hash: str
    path: Path

    @property
    def venv(self) -> Path:
        """产物 venv 目录。"""
        return self.path / "venv"

    @property
    def python(self) -> Path:
        """产物 venv 里的解释器。"""
        return self.venv / "bin" / "python"

    @property
    def complete(self) -> bool:
        """产物是否已构建完成且解释器可用。"""
        return (self.path / COMPLETE_MARKER).is_file() and self.python.exists()

    def manifest(self) -> dict[str, object]:
        """读取 ``manifest.json``；缺失或损坏时返回空 dict。"""
        try:
            raw: object = json.loads(
                (self.path / MANIFEST_NAME).read_text(encoding="utf-8")
            )
        except OSError, ValueError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return cast("dict[str, object]", raw)


def artifact_hash(
    service: DesiredService, desired: DesiredState, *, python: str
) -> str:
    """计算产物的"闭包指纹"。

    除了服务自己的源码与版本，还必须包含 **SDK、依赖契约、``uv.lock`` 与解释器
    版本**——因为产物 venv 里装的就是这几样东西。只算服务目录的话，改了 SDK 或
    升级依赖后指纹不变，会复用旧 venv，服务继续跑旧代码。

    :param service: 目标服务
    :param desired: 目标服务所属的期望状态
    :param python: 建 venv 用的解释器版本字符串（由 uv 解析）
    """
    contracts = {
        name: [
            desired.contracts[name].package_version,
            desired.contracts[name].source_hash,
        ]
        for name in sorted(service.meta.depends_contracts)
        if name in desired.contracts
    }
    packages = {
        name: [
            desired.sdk.packages[name].package_version,
            desired.sdk.packages[name].source_hash,
        ]
        for name in sorted(service.meta.depends_sdk)
        if name in desired.sdk.packages
    }
    payload = {
        "service": service.meta.name,
        "package": service.meta.package,
        "module": service.meta.module,
        "command": list(service.meta.command),
        "source_hash": service.source_hash,
        "package_version": service.package_version,
        "sdk": [desired.sdk.package_version, desired.sdk.source_hash],
        "sdk_packages": packages,
        "contracts": contracts,
        "deps_hash": desired.deps_hash,
        "python": python,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def venv_site_packages(venv: Path) -> Path:
    """返回 venv 的 ``site-packages`` 目录（按实际解释器版本推断）。

    :raises SupervisorError: venv 结构不认识（没有 ``lib/pythonX.Y``）。
    """
    lib = venv / "lib"
    if not lib.is_dir():
        raise SupervisorError(f"venv 结构异常，缺少 lib/：{venv}")
    candidates = sorted(
        path for path in lib.iterdir() if path.name.startswith("python")
    )
    if not candidates:
        raise SupervisorError(f"venv 结构异常，缺少 lib/pythonX.Y/：{venv}")
    return candidates[-1] / "site-packages"


class ArtifactStore:
    """产物的构建、查询与回收。"""

    def __init__(
        self,
        state_dir: Path,
        *,
        project_root: Path,
        python: str = "3.14",
        link_mode: str = "hardlink",
        warn_cross_device: bool = True,
    ) -> None:
        self._state_dir = state_dir
        self._project_root = project_root
        self._python = python
        self._link_mode = link_mode
        self._warn_cross_device = warn_cross_device

    @property
    def python_version(self) -> str:
        """建 venv 用的解释器版本。"""
        return self._python

    @property
    def root(self) -> Path:
        """产物根目录（``.blue/artifacts``）。"""
        return self._state_dir / "artifacts"

    @property
    def wheels_root(self) -> Path:
        """wheel 缓存根目录（``.blue/wheels``，按源码指纹分目录）。"""
        return self._state_dir / "wheels"

    @property
    def base_root(self) -> Path:
        """共享依赖 base 根目录（按 ``uv.lock`` 指纹分目录）。"""
        return self.root / "_base"

    def hash_for(self, service: DesiredService, desired: DesiredState) -> str:
        """计算该服务在当前期望状态下的产物指纹。"""
        return artifact_hash(service, desired, python=self._python)

    def resolve(self, service: str, digest: str) -> ArtifactRef:
        """按名字与指纹定位产物（**不构建**）。"""
        return ArtifactRef(
            service=service, hash=digest, path=self.root / service / digest
        )

    def ensure(self, service: DesiredService, desired: DesiredState) -> ArtifactRef:
        """确保产物存在；必要时构建。

        用 ``meta.command`` 自定义启动命令的服务（外部脚本/二进制）不产出 Python
        产物，直接返回一个未完成的引用（调用方据此退回原启动方式）。

        :raises SupervisorError: 构建失败
        """
        digest = self.hash_for(service, desired)
        ref = self.resolve(service.meta.name, digest)
        if service.meta.command:
            return ref
        if ref.complete:
            return ref
        self._build(service, desired, ref)
        return ref

    def reject_editable_installs(self, site_packages: Path | None = None) -> None:
        """拒绝工作区之外的 editable 安装——它们的内容无法被产物固化。

        ``site_packages`` 缺省时扫描开发用 ``.venv``；该 venv 不存在就直接放行
        （此时并没有东西可扫，真需要它时 :meth:`_workspace_venv` 会给出更准的报错）。

        :raises SupervisorError: 存在工作区外的 editable 安装
        """
        if site_packages is None:
            workspace_venv = self._project_root / ".venv"
            if not (workspace_venv / "lib").is_dir():
                return
            site_packages = venv_site_packages(workspace_venv)
        found = editable_installs(site_packages, project_root=self._project_root)
        if not found:
            return
        lines = [
            "检测到工作区之外的 editable 安装，产物无法固化它们的代码：",
            *(f"  - {name} → {target}" for name, target in found),
            "",
            "editable 依赖在 base 里只有两种下场，都不能接受：",
            "  1. .pth 指针被跳过 → base 缺模块，服务启动即 ImportError；",
            "  2. 保留指针 → 产物跟着上游源码漂移，artifact_hash 与内容脱钩。",
            "",
            "处理办法（二选一）：",
            "  - 已发版：删掉 [tool.uv.sources] 的 path 条目，再 uv sync；",
            "  - 未发版：uv build 出 wheel，放进 .blue/wheels 走 --find-links。",
        ]
        raise SupervisorError("\n".join(lines))

    def ensure_base(self, desired: DesiredState) -> Path:
        """确保共享依赖 base 存在；返回它的 ``site-packages`` 目录。

        base 由开发用 ``.venv`` 硬链接克隆而来（排除 workspace 包），按
        ``uv.lock`` 指纹命名：依赖不变就一直复用，变了就克隆一份新的。

        克隆前先扫一遍 editable 安装：工作区之外的 editable 依赖无法固化，宁可
        在这里失败，也不要让 base 里悄悄少一个模块。

        :raises SupervisorError: ``.venv`` 缺失、含工作区外 editable 安装或克隆失败
        """
        key = desired.deps_hash or "nolock"
        base = self.base_root / key
        marker = base / BASE_MARKER
        if marker.is_file():
            return venv_site_packages(base)

        workspace_venv = self._workspace_venv()
        source = venv_site_packages(workspace_venv)
        self.reject_editable_installs(source)
        self._warn_if_cross_device(source, self.base_root)
        staging = base.with_name(f"{base.name}{_STAGING_MARK}{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        target = staging / "lib" / source.parent.name / "site-packages"
        target.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir()):
            if _skip_site_packages_entry(entry.name):
                continue
            link_tree(entry, target / entry.name)
        (staging / BASE_MARKER).write_text("ok\n", encoding="utf-8")
        base.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(base, ignore_errors=True)
        staging.rename(base)
        return venv_site_packages(base)

    def gc(self, keep: dict[str, set[str]], *, dry_run: bool = False) -> list[str]:
        """回收没有被任何保留世代引用的产物。

        :param keep: ``{服务名: 要保留的产物指纹集合}``
        :param dry_run: 只列出、不删除
        :return: 命中的产物（相对 ``artifacts`` 的路径）
        """
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        for service_dir in sorted(self.root.iterdir()):
            if not service_dir.is_dir() or service_dir.name == "_base":
                continue
            keep_hashes = keep.get(service_dir.name, set())
            for artifact_dir in sorted(service_dir.iterdir()):
                if not artifact_dir.is_dir() or artifact_dir.name in keep_hashes:
                    continue
                if _is_staging(artifact_dir):
                    # 别的进程正在往里写（<hash>.tmp-<pid>），删了就是把人家 build 打崩
                    continue
                if not dry_run:
                    shutil.rmtree(artifact_dir, ignore_errors=True)
                removed.append(f"{service_dir.name}/{artifact_dir.name}")
            if not dry_run:
                # 目录可能刚被并发构建创建出来；竞争下 rmdir 失败不该打断整轮回收
                with contextlib.suppress(OSError):
                    service_dir.rmdir()
        return removed

    def gc_bases(self, keep_keys: set[str], *, dry_run: bool = False) -> list[str]:
        """回收不用的 base（调用方先算好要保留的 ``uv.lock`` 指纹集合）。"""
        removed: list[str] = []
        if not self.base_root.is_dir():
            return removed
        for base_dir in sorted(self.base_root.iterdir()):
            if not base_dir.is_dir() or base_dir.name in keep_keys:
                continue
            if _is_staging(base_dir):
                # 另一个进程正在克隆 base，删了它就得从零重来
                continue
            if not dry_run:
                shutil.rmtree(base_dir, ignore_errors=True)
            removed.append(f"_base/{base_dir.name}")
        return removed

    def keep_wheel_keys(self, keep: dict[str, set[str]]) -> set[str] | None:
        """保留产物引用到的**源码指纹**——``wheels`` 目录就是按它命名的。

        wheel 目录键是"每个包各自的 ``source_hash``"（blue SDK / 各 sdk 包 / 各契约 /
        服务自己），所以不能只看服务自己的 ``source_hash``。权威来源是产物里的
        ``manifest.json``（产物一旦 ``.complete`` 就不可变，读它最准）。

        :return: 要保留的键；**有任何清单读不出来或结构认不出时返回 ``None``**，
            表示无法判定——调用方应当一个都不删。宁可留垃圾，也不要删掉活产物
            还在引用的 wheel。
        """
        # ``_ensure_wheels`` 在指纹为空时用 ``unknown`` 兜底，那个目录一律保留
        keys: set[str] = {"unknown"}
        for service, hashes in keep.items():
            for digest in hashes:
                manifest = self.root / service / digest / MANIFEST_NAME
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    logger.warning(
                        "读不出产物清单 %s，跳过 wheel 回收判定：%s", manifest, error
                    )
                    return None
                if not isinstance(data, dict):
                    logger.warning(
                        "产物清单 %s 不是对象，跳过 wheel 回收判定", manifest
                    )
                    return None
                hashes_in_manifest = _manifest_hashes(
                    cast(dict[str, object], data), origin=manifest
                )
                if hashes_in_manifest is None:
                    return None
                keys.update(hashes_in_manifest)
        keys.discard("")
        return keys

    def gc_wheels(
        self, keep_keys: set[str] | None, *, dry_run: bool = False
    ) -> list[str]:
        """回收没人引用的 wheel 目录（``.blue/wheels/<源码指纹>/``）。

        :param keep_keys: :meth:`keep_wheel_keys` 的结果；``None`` = 判定不了，跳过
        :return: 命中的目录（相对 ``wheels``）
        """
        if keep_keys is None or not self.wheels_root.is_dir():
            return []
        removed: list[str] = []
        for directory in sorted(self.wheels_root.iterdir()):
            if not directory.is_dir() or directory.name in keep_keys:
                continue
            if not dry_run:
                shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory.name)
        return removed

    # -- 构建 ---------------------------------------------------------------

    def _build(
        self, service: DesiredService, desired: DesiredState, ref: ArtifactRef
    ) -> None:
        """构建一个产物：造 wheel → 建 venv → 装包 → 落 manifest 与完成标记。"""
        staging = ref.path.with_name(f"{ref.path.name}{_STAGING_MARK}{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        base_site = self.ensure_base(desired)
        wheels = self._ensure_wheels(service, desired)
        venv = staging / "venv"
        self._run(
            ["uv", "venv", str(venv), "--relocatable", "--python", self._python],
            cwd=self._project_root,
        )
        site = venv_site_packages(venv)
        (site / "zz_blue_base.pth").write_text(f"{base_site}\n", encoding="utf-8")
        install = [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--offline",
            "--no-deps",
            f"--link-mode={self._link_mode}",
        ]
        for directory in wheels:
            install.extend(["--find-links", str(directory)])
        install.extend(_install_targets(service, desired))
        self._run(install, cwd=self._project_root)
        manifest = {
            "version": MANIFEST_VERSION,
            "service": service.meta.name,
            "artifact_hash": ref.hash,
            "package": service.meta.package,
            "module": service.meta.module,
            "package_version": service.package_version,
            "source_hash": service.source_hash,
            "sdk": {
                "package_version": desired.sdk.package_version,
                "source_hash": desired.sdk.source_hash,
            },
            "sdk_packages": {
                name: {
                    "package": desired.sdk.packages[name].meta.package,
                    "module": desired.sdk.packages[name].meta.module,
                    "package_version": desired.sdk.packages[name].package_version,
                    "source_hash": desired.sdk.packages[name].source_hash,
                }
                for name in sorted(service.meta.depends_sdk)
                if name in desired.sdk.packages
            },
            "contracts": {
                name: desired.contracts[name].source_hash
                for name in sorted(service.meta.depends_contracts)
                if name in desired.contracts
            },
            "deps_hash": desired.deps_hash,
            "python": self._python,
            "base": str(base_site),
            "interpreter": sys.executable,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")
        ref.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(ref.path, ignore_errors=True)
        staging.rename(ref.path)

    def _ensure_wheels(
        self, service: DesiredService, desired: DesiredState
    ) -> list[Path]:
        """确保产物要装的 workspace 包都有 wheel，返回各自的 ``--find-links`` 目录。"""
        targets: list[tuple[str, str]] = [
            ("blue", desired.sdk.source_hash),
            *(
                (
                    desired.sdk.packages[name].meta.package,
                    desired.sdk.packages[name].source_hash,
                )
                for name in sorted(service.meta.depends_sdk)
                if name in desired.sdk.packages
            ),
            *(
                (
                    desired.contracts[name].meta.package,
                    desired.contracts[name].source_hash,
                )
                for name in sorted(service.meta.depends_contracts)
                if name in desired.contracts
            ),
            (service.meta.package, service.source_hash),
        ]
        directories: list[Path] = []
        built: set[str] = set()
        for package, digest in targets:
            key = digest or "unknown"
            directory = self.wheels_root / key
            if key in built or _has_wheel(directory):
                built.add(key)
                if directory not in directories:
                    directories.append(directory)
                continue
            self.wheels_root.mkdir(parents=True, exist_ok=True)
            self._run(
                ["uv", "build", "--package", package, "--out-dir", str(directory)],
                cwd=self._project_root,
            )
            built.add(key)
            directories.append(directory)
        return directories

    def _workspace_venv(self) -> Path:
        """定位开发用 venv（``.venv``）。

        :raises SupervisorError: 找不到——共享 base 只能从已同步的开发环境克隆
        """
        venv = self._project_root / ".venv"
        if not (venv / "lib").is_dir():
            raise SupervisorError(
                "找不到开发用 .venv，无法构建产物依赖基座；请先执行 uv sync --group dev"
            )
        return venv

    def _warn_if_cross_device(self, source: Path, target: Path) -> None:
        """硬链接跨分区会静默退化成拷贝，磁盘成倍增长——告警一次。"""
        if not self._warn_cross_device:
            return
        try:
            source_dev = os.stat(source).st_dev
            target.parent.mkdir(parents=True, exist_ok=True)
            target_dev = os.stat(target.parent).st_dev
        except OSError:
            return
        if source_dev != target_dev:
            print(
                "⚠️ 产物目录与开发用 .venv 不在同一文件系统：硬链接会退化成拷贝，"
                f"每个产物都会真实占用一份依赖空间（{source} → {target}）",
                file=sys.stderr,
            )

    def _run(self, args: list[str], *, cwd: Path) -> None:
        """执行外部命令；失败时把 stderr 尾部带进异常。

        :raises SupervisorError: 命令不存在或返回非零
        """
        executable = shutil.which(args[0])
        if executable is None:
            raise SupervisorError(f"未找到可执行文件：{args[0]}")
        result = subprocess.run(
            [executable, *args[1:]],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-8:]
            raise SupervisorError(
                f"命令失败（{result.returncode}）：{' '.join(args)}\n"
                + "\n".join(detail)
            )


def _skip_site_packages_entry(name: str) -> bool:
    """共享 base 不该包含的 site-packages 条目。"""
    if name in _SKIP_SITE_PACKAGES:
        return True
    if name.startswith(_SKIP_PREFIXES):
        return True
    return name.endswith(".pth")


def editable_installs(
    site_packages: Path, *, project_root: Path
) -> list[tuple[str, Path]]:
    """扫描 ``site-packages``，返回**工作区之外**的 editable 安装。

    判据是 PEP 610 的 ``<dist-info>/direct_url.json``（``dir_info.editable = true``），
    外加老式 setuptools 的 ``*.egg-link``——这两个是"这里是 editable 安装"的明确
    记录。**不**按 ``.pth`` 文件名去猜：各家工具的命名不一样，而普通 ``.pth``
    本身也是合法文件。

    工作区内的 editable 安装不算问题：uv workspace 成员会以 **wheel** 形式装进每个
    产物，:func:`_skip_site_packages_entry` 也保证它们的指针不落进共享 base。

    工作区**之外**的 editable 依赖则无法固化，而且两种下场都不可接受：

    1. 它的 ``.pth`` 指针被跳过 → base 里没有这个模块，服务启动即 ``ImportError``；
    2. 保留指针 → 产物跟着上游工作区源码漂移，``artifact_hash`` 与实际内容脱钩。

    所以调用方应当直接失败，而不是让"少一个模块"静默发生。

    :param site_packages: 开发用 venv 的 ``site-packages`` 目录
    :param project_root: 项目根；落在这个目录内的 editable 安装视为工作区成员
    :return: ``[(包名, 目标目录), ...]``，按包名排序
    """
    found: list[tuple[str, Path]] = []
    if not site_packages.is_dir():
        return found
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        record = dist_info / _DIRECT_URL_RECORD
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if not isinstance(data, dict):
            continue
        payload = cast(dict[str, object], data)
        dir_info = payload.get("dir_info")
        if not isinstance(dir_info, dict):
            continue
        if not cast(dict[str, object], dir_info).get("editable"):
            continue
        target = _url_to_path(str(payload.get("url") or ""))
        if target is None or _is_within(target, project_root):
            continue
        found.append((_distribution_name(dist_info), target))
    for egg_link in sorted(site_packages.glob("*.egg-link")):
        target = _egg_link_target(egg_link)
        if target is None or _is_within(target, project_root):
            continue
        found.append((egg_link.name.removesuffix(".egg-link"), target))
    found.sort()
    return found


def _distribution_name(dist_info: Path) -> str:
    """``infinity_bus-6.2.0.dist-info`` → ``infinity_bus``。

    版本号里不会出现 ``-``（PEP 440 的本地版本用 ``+``），所以从右切一次即可，
    不会把 ``infinity-bus`` 这种带横线的包名切坏。
    """
    return dist_info.name.removesuffix(".dist-info").rsplit("-", 1)[0]


def _url_to_path(url: str) -> Path | None:
    """PEP 610 的 ``file://`` URL → 本地路径；非本地 URL 返回 ``None``。"""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(parsed.path))


def _egg_link_target(egg_link: Path) -> Path | None:
    """``*.egg-link`` 的首行是源码目录（setuptools 的旧式 editable 标记）。"""
    try:
        lines = egg_link.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.strip():
            return Path(line.strip())
    return None


def _is_within(path: Path, root: Path) -> bool:
    """``path`` 是否落在 ``root`` 之内（解析符号链接后比较）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except OSError, ValueError:
        return False
    return True


def _is_staging(path: Path) -> bool:
    """是否是构建中的临时目录——另一个进程正在写，回收必须让开。"""
    return _STAGING_MARK in path.name


def _warn_unknown_manifest(origin: Path, problem: str) -> None:
    """清理类操作遇到读不懂的清单时只告警、不猜——猜错会删掉还在用的 wheel。"""
    logger.warning("产物清单 %s %s，跳过 wheel 回收判定", origin, problem)


def _manifest_hashes(manifest: dict[str, object], *, origin: Path) -> set[str] | None:
    """产物清单里出现的全部源码指纹——决定哪些 wheel 目录还被需要。

    wheel 目录按**每个包各自的** ``source_hash`` 命名，所以服务自己的
    ``source_hash`` 不够：blue SDK、各 sdk 包、各契约包各有各的键。

    清单在 ``.complete`` 之后不再改写，所以这里没有"历史格式"要兼容：字段形态
    只有一个（见 :meth:`ArtifactStore._build`），认不出来就说明写坏了或升了版。

    :param origin: 清单路径，仅用于告警定位
    :return: 指纹集合；**结构认不出时返回 ``None`` 并告警**，调用方应当一个都不删
    """
    version = manifest.get("version")
    if version != MANIFEST_VERSION:
        _warn_unknown_manifest(
            origin, f"版本不认识（{version!r}，期望 {MANIFEST_VERSION}）"
        )
        return None
    hashes: set[str] = set()
    direct = manifest.get("source_hash")
    if not isinstance(direct, str):
        _warn_unknown_manifest(origin, "缺少 source_hash")
        return None
    hashes.add(direct)
    # blue SDK：{"package_version": ..., "source_hash": ...}
    sdk = manifest.get("sdk")
    sdk_hash = (
        cast(dict[str, object], sdk).get("source_hash")
        if isinstance(sdk, dict)
        else None
    )
    if not isinstance(sdk_hash, str):
        _warn_unknown_manifest(origin, "的 sdk 字段缺少 source_hash")
        return None
    hashes.add(sdk_hash)
    # 各 sdk 包：{名字: {"package": ..., "source_hash": ...}}——值是对象，不是字符串
    packages = manifest.get("sdk_packages")
    if not isinstance(packages, dict):
        _warn_unknown_manifest(origin, "的 sdk_packages 字段不是对象")
        return None
    for name, entry in cast(dict[str, object], packages).items():
        entry_hash = (
            cast(dict[str, object], entry).get("source_hash")
            if isinstance(entry, dict)
            else None
        )
        if not isinstance(entry_hash, str):
            _warn_unknown_manifest(origin, f"的 sdk_packages[{name}] 缺少 source_hash")
            return None
        hashes.add(entry_hash)
    # 各契约：{名字: source_hash}——值就是字符串
    contracts = manifest.get("contracts")
    if not isinstance(contracts, dict):
        _warn_unknown_manifest(origin, "的 contracts 字段不是对象")
        return None
    for name, value in cast(dict[str, object], contracts).items():
        if not isinstance(value, str):
            _warn_unknown_manifest(origin, f"的 contracts[{name}] 不是字符串指纹")
            return None
        hashes.add(value)
    hashes.discard("")
    return hashes


def _install_targets(service: DesiredService, desired: DesiredState) -> list[str]:
    """产物 venv 里要装的包名（精确到声明过的依赖，不多装）。"""
    targets = [
        "blue",
        *(
            desired.sdk.packages[name].meta.package
            for name in sorted(service.meta.depends_sdk)
            if name in desired.sdk.packages
        ),
    ]
    targets.extend(
        desired.contracts[name].meta.package
        for name in sorted(service.meta.depends_contracts)
        if name in desired.contracts
    )
    targets.append(service.meta.package)
    return targets


def _has_wheel(directory: Path) -> bool:
    """该目录里是否已经有 wheel（``uv build`` 的产物）。"""
    return directory.is_dir() and any(directory.glob("*.whl"))


def link_tree(source: Path, target: Path) -> None:
    """把 ``source`` 尽量以硬链接方式放到 ``target``（跨分区退化为拷贝）。

    硬链接让 25 个产物共享同一份依赖 inode；拷贝只是慢与占空间，语义不变。
    """
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            link_tree(entry, target / entry.name)
        return
    if target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
