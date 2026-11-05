"""世代（generation）：不可变的期望状态快照 + 单点原子激活指针。

一次 ``blue apply`` 产出一个世代目录，里面同时冻结**配置**（每个服务的
``service_runtime_config.json``）与**产物引用**（artifact 指纹）。切换世代只做一件
事：用 ``os.replace`` 原子替换 ``.blue/current`` 符号链接。于是：

- 激活是单点原子操作，不存在"一半服务用新配置、一半用旧配置"的窗口；
- 回滚是"换指针"，不依赖上一份 ``blue.infd``（世代目录是自足的）；
- 崩溃后据 ``.stable`` 标记判断"上次激活有没有走完"，自动退到上一个稳定世代。

数据面（会话库、RabbitMQ 数据、用户文件）**不在世代里**，回滚不会碰它们。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blue.control.artifacts import ArtifactStore
from blue.control.desired import render_state, write_runtime_configs
from blue.control.model import (
    ContractMeta,
    DesiredContract,
    DesiredSdk,
    DesiredService,
    DesiredState,
    ServiceMeta,
    SupervisorSettings,
)
from blue.errors import BlueError

__all__ = [
    "COMPLETE_MARKER",
    "CURRENT_LINK",
    "FAILED_MARKER",
    "GENERATION_FILE",
    "STABLE_MARKER",
    "Generation",
    "GenerationStore",
]

GENERATION_FILE = "generation.json"
"""世代元数据文件名。"""

COMPLETE_MARKER = ".complete"
"""目录写完的标记；缺它说明是半成品，禁止激活。"""

STABLE_MARKER = ".stable"
"""激活并验证通过的标记；缺它说明上次激活没走完。"""

FAILED_MARKER = ".failed"
"""曾经激活失败（已回滚）的标记，只用于展示与排障。"""

CURRENT_LINK = "current"
"""指向当前世代的符号链接名（位于 state_dir 下）。"""

DESIRED_SNAPSHOT = "desired.json"
"""人类可读的期望状态快照（密钥已脱敏）。"""

CONFIG_SECTION = "config"
INFRA_SECTION = "infra"


class _ContractEntry(BaseModel):
    """世代里记录的契约元数据（重建 ``DesiredContract`` 用）。"""

    model_config = ConfigDict(extra="forbid")

    package: str
    module: str
    description: str = ""
    package_version: str = ""
    source_hash: str = ""


class _ServiceEntry(BaseModel):
    """世代里记录的一个服务：产物指纹 + 元数据 + 配置指纹。"""

    model_config = ConfigDict(extra="forbid")

    artifact_hash: str = ""
    meta: ServiceMeta
    config_hash: str
    package_version: str = ""
    source_hash: str = ""


class _GenerationFile(BaseModel):
    """``generation.json`` 的结构。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 2
    id: int
    generation: str
    parent_id: int | None = None
    created_at: float
    created_by: str = ""
    reason: str = ""
    sdk: DesiredSdk = Field(default_factory=DesiredSdk)
    contracts: dict[str, _ContractEntry] = Field(default_factory=dict)
    deps_hash: str = ""
    bus_url: str = ""
    bus_transport: Literal["rabbit", "memory"] = "rabbit"
    supervisor: SupervisorSettings | None = None
    """冻结时的控制面设置。

    **必须随世代一起冻结**：``activation_timeout`` / ``generation_auto_rollback``
    这类参数决定激活与回滚的行为，属于"这一代怎么跑"的一部分。只存在工作区的
    ``blue.infd`` 里会出事——``apply --config other.infd`` 改了这些值，调谐器却仍在
    用自己那个 ``config_path`` 编译出来的旧值（实测：把 activation_timeout 从
    120s 改成 12s 后，日志里仍是 120s）。
    """

    services: dict[str, _ServiceEntry] = Field(default_factory=dict)


@dataclass(frozen=True)
class Generation:
    """一个已落盘的世代（不可变）。"""

    path: Path
    data: _GenerationFile

    @property
    def id(self) -> int:
        """世代号（单调整数）。"""
        return self.data.id

    @property
    def generation(self) -> str:
        """内容指纹（``DesiredState.generation``）。"""
        return self.data.generation

    @property
    def parent_id(self) -> int | None:
        """上一个世代号。"""
        return self.data.parent_id

    @property
    def services(self) -> dict[str, _ServiceEntry]:
        """该世代的全部服务。"""
        return self.data.services

    @property
    def complete(self) -> bool:
        """目录是否写完。"""
        return (self.path / COMPLETE_MARKER).is_file()

    @property
    def stable(self) -> bool:
        """是否已通过激活验证。"""
        return (self.path / STABLE_MARKER).is_file()

    @property
    def failed(self) -> bool:
        """是否曾经激活失败并被回滚。"""
        return (self.path / FAILED_MARKER).is_file()

    @property
    def state(self) -> str:
        """``stable`` / ``failed`` / ``pending``，供展示。"""
        if self.stable:
            return "stable"
        if self.failed:
            return "failed"
        return "pending"

    def config_path(self, service: str) -> Path:
        """该服务在这个世代里的运行时配置文件路径。"""
        return self.path / "services" / service / "service_runtime_config.json"

    def to_desired(
        self,
        *,
        supervisor: SupervisorSettings,
        base_dir: Path,
        artifacts: ArtifactStore,
        strict: bool = True,
    ) -> DesiredState:
        """把世代重建为可调谐的 :class:`DesiredState`。

        不需要重新编译 ``blue.infd``：配置从世代目录读、产物按指纹定位、控制面
        设置也用**世代里冻结的那份**（老世代没有存设置时退回 ``supervisor`` 参数）。

        :param supervisor: 兜底的控制面设置（来自 ``blue.infd`` 的 bootstrap 编译）
        :param base_dir: 项目根
        :param artifacts: 产物仓库（用于定位解释器）
        :param strict: 产物缺失时是否报错；``False`` 时退回 ``uv run``
        :raises BlueError: ``strict`` 且产物缺失
        """
        settings = self.data.supervisor or supervisor
        contracts = {
            name: DesiredContract(
                meta=ContractMeta(
                    name=name,
                    package=entry.package,
                    module=entry.module,
                    description=entry.description,
                ),
                package_version=entry.package_version,
                source_hash=entry.source_hash,
                contract_dir=base_dir / "contracts" / name,
                contract_path=base_dir / "contracts" / name / "contract.infd",
            )
            for name, entry in self.data.contracts.items()
        }
        services: dict[str, DesiredService] = {}
        for name, entry in self.data.services.items():
            config_path = self.config_path(name)
            payload = self._read_config(config_path)
            python: Path | None = None
            if entry.artifact_hash:
                ref = artifacts.resolve(name, entry.artifact_hash)
                if ref.complete:
                    python = ref.python
                elif strict:
                    raise BlueError(
                        f"世代 {self.id} 引用的产物已不存在：{name}"
                        f"/{entry.artifact_hash[:12]}"
                        "（先 blue apply 重建，或用更早的世代）"
                    )
            services[name] = DesiredService(
                meta=entry.meta,
                config=payload,
                config_hash=entry.config_hash,
                package_version=entry.package_version,
                source_hash=entry.source_hash,
                service_dir=base_dir / "services" / name,
                config_path=config_path,
                cwd=base_dir,
                artifact_hash=entry.artifact_hash,
                artifact_python=python,
                generation_id=self.id,
            )
        return DesiredState(
            generation=self.data.generation,
            config_path=base_dir / "blue.infd",
            base_dir=base_dir,
            supervisor=settings,
            services=services,
            contracts=contracts,
            sdk=self.data.sdk,
            bus_url=self.data.bus_url,
            bus_transport=self.data.bus_transport,
            deps_hash=self.data.deps_hash,
        )

    def _read_config(self, path: Path) -> dict[str, object]:
        """读世代里的服务配置，取出 ``config`` 段。

        :raises BlueError: 文件缺失或结构不认识
        """
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise BlueError(f"世代 {self.id} 缺少服务配置：{path}") from error
        except ValueError as error:
            raise BlueError(
                f"世代 {self.id} 的服务配置不是合法 JSON：{path}"
            ) from error
        if not isinstance(raw, dict):
            raise BlueError(f"世代 {self.id} 的服务配置结构异常：{path}")
        payload = cast("dict[str, object]", raw)
        config = payload.get(CONFIG_SECTION)
        if not isinstance(config, dict):
            return {}
        return cast("dict[str, object]", config)


class GenerationStore:
    """世代目录、``current`` 指针与生命周期管理。"""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    @property
    def state_dir(self) -> Path:
        """状态目录（``.blue``）。"""
        return self._state_dir

    @property
    def root(self) -> Path:
        """世代根目录（``.blue/generations``）。"""
        return self._state_dir / "generations"

    @property
    def current_link(self) -> Path:
        """``current`` 符号链接路径。"""
        return self._state_dir / CURRENT_LINK

    def ensure_dirs(self) -> None:
        """创建世代目录（权限 0700：里面有明文密钥）。"""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)

    def list(self) -> list[Generation]:
        """列出全部世代（按世代号升序；跳过半成品目录）。"""
        if not self.root.is_dir():
            return []
        generations: list[Generation] = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or not path.name.isdigit():
                continue
            loaded = self._load_path(path)
            if loaded is not None:
                generations.append(loaded)
        return generations

    def load(self, generation_id: int) -> Generation:
        """按世代号加载。

        :raises BlueError: 不存在或元数据损坏
        """
        path = self.root / f"{generation_id:04d}"
        loaded = self._load_path(path)
        if loaded is None:
            raise BlueError(f"世代不存在或已损坏：{generation_id}")
        return loaded

    def current(self) -> Generation | None:
        """读取 ``current`` 指向的世代；没有指针或指向已删除目录时返回 ``None``。"""
        link = self.current_link
        if not link.is_symlink():
            return None
        try:
            target = link.resolve(strict=True)
        except OSError:
            return None
        return self._load_path(target)

    def latest_stable(self, *, exclude: int | None = None) -> Generation | None:
        """最新的 stable 世代（可排除某个世代号）。"""
        for generation in reversed(self.list()):
            if generation.id == exclude or not generation.stable:
                continue
            return generation
        return None

    def create(
        self,
        desired: DesiredState,
        artifacts: ArtifactStore,
        *,
        reason: str,
        created_by: str = "",
    ) -> Generation:
        """把一份期望状态冻结成新世代目录（**不激活**）。

        内容与 ``current`` 完全一致且已 stable 时直接复用，不产生新世代。

        :raises BlueError: 写入失败
        """
        self.ensure_dirs()
        existing = self.list()
        current = self.current()
        if current is not None and current.generation == desired.generation:
            # 内容一致就复用：Nix 的 "同样输入 → 同样 store path"
            return current

        generation_id = (existing[-1].id + 1) if existing else 1
        parent_id = existing[-1].id if existing else None
        staging = self.root / f"{generation_id:04d}.tmp-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        (staging / "services").mkdir(parents=True, exist_ok=True)

        clone = desired.model_copy(
            update={
                "services": {
                    name: service.model_copy(
                        update={
                            "config_path": staging
                            / "services"
                            / name
                            / "service_runtime_config.json"
                        }
                    )
                    for name, service in desired.services.items()
                }
            }
        )
        write_runtime_configs(clone, only_changed=False)
        (staging / DESIRED_SNAPSHOT).write_text(
            json.dumps(render_state(desired), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        payload = _GenerationFile(
            id=generation_id,
            generation=desired.generation,
            parent_id=parent_id,
            created_at=_now(),
            created_by=created_by,
            reason=reason,
            sdk=desired.sdk,
            contracts={
                name: _ContractEntry(
                    package=contract.meta.package,
                    module=contract.meta.module,
                    description=contract.meta.description,
                    package_version=contract.package_version,
                    source_hash=contract.source_hash,
                )
                for name, contract in desired.contracts.items()
            },
            deps_hash=desired.deps_hash,
            bus_url=desired.bus_url,
            bus_transport=desired.bus_transport,
            supervisor=desired.supervisor,
            services={
                name: _ServiceEntry(
                    artifact_hash=artifacts.hash_for(service, desired)
                    if not service.meta.command
                    else "",
                    meta=service.meta,
                    config_hash=service.config_hash,
                    package_version=service.package_version,
                    source_hash=service.source_hash,
                )
                for name, service in desired.services.items()
            },
        )
        (staging / GENERATION_FILE).write_text(
            payload.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (staging / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")

        final = self.root / f"{generation_id:04d}"
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
        return self.load(generation_id)

    def activate(self, generation: Generation) -> None:
        """原子切换 ``current`` 到该世代。

        先把链接写到临时名，再用 ``os.replace`` 覆盖——替换符号链接是原子的。

        :raises BlueError: 目录未完成或链接创建失败
        """
        if not generation.complete:
            raise BlueError(f"世代 {generation.id} 不完整，拒绝激活")
        self.ensure_dirs()
        tmp = self.state_dir / f"{CURRENT_LINK}.tmp-{os.getpid()}"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        target = os.path.relpath(generation.path, self.state_dir)
        try:
            os.symlink(target, tmp)
            os.replace(tmp, self.current_link)
        except OSError as error:
            tmp.unlink(missing_ok=True)
            raise BlueError(f"切换 current 失败：{error}") from error

    def mark_stable(self, generation: Generation, *, tools: int | None = None) -> None:
        """标记世代已验证通过。

        ``.stable`` 有**两个写者**：调谐器的健康闸门（不带观测值）和 CLI 的冒烟
        （带工具登记数）。后来者不能抹掉已有内容，否则观测值会被闸门那次覆盖掉。

        :param tools: 冒烟观测到的工具数；``None`` 表示"只标通过，别动已有内容"
        """
        marker = generation.path / STABLE_MARKER
        (generation.path / FAILED_MARKER).unlink(missing_ok=True)
        if tools is None and marker.is_file():
            return
        payload = (
            json.dumps({"tools": tools, "checked_at": time.time()}, ensure_ascii=False)
            if tools is not None
            else "ok"
        )
        marker.write_text(payload + "\n", encoding="utf-8")

    def mark_failed(self, generation: Generation, message: str = "") -> None:
        """标记世代激活失败（已回滚），原因写进同目录的 ``.failed``。"""
        (generation.path / FAILED_MARKER).write_text(
            (message or "activation failed") + "\n", encoding="utf-8"
        )
        (generation.path / STABLE_MARKER).unlink(missing_ok=True)

    def rebind_service(
        self, *, service: str, source: Generation, created_by: str = ""
    ) -> Generation:
        """生成一个新世代：内容与当前世代相同，但把 ``service`` 换成 ``source`` 的。

        单服务回滚就是这么实现的——历史保持线性、可审计，不会出现指针来回跳。

        :raises BlueError: 没有当前世代，或任一世代里没有该服务
        """
        current = self.current()
        if current is None:
            raise BlueError("没有当前世代，无法单服务回滚；先执行 blue apply")
        if service not in current.services:
            raise BlueError(f"当前世代里没有服务 {service!r}")
        if service not in source.services:
            raise BlueError(f"世代 {source.id} 里没有服务 {service!r}")

        self.ensure_dirs()
        existing = self.list()
        generation_id = (existing[-1].id + 1) if existing else 1
        staging = self.root / f"{generation_id:04d}.tmp-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        (staging / "services").mkdir(parents=True, exist_ok=True)
        for name in current.services:
            origin = (
                source.config_path(name)
                if name == service and source.config_path(name).is_file()
                else current.config_path(name)
            )
            if origin.is_file():
                destination = staging / "services" / name
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination / origin.name)
        snapshot = current.path / DESIRED_SNAPSHOT
        if snapshot.is_file():
            shutil.copy2(snapshot, staging / DESIRED_SNAPSHOT)

        services = dict(current.data.services)
        services[service] = source.data.services[service]
        payload = current.data.model_copy(
            update={
                "id": generation_id,
                "generation": _rebind_hash(current, source, service),
                "parent_id": current.id,
                "created_at": _now(),
                "created_by": created_by,
                "reason": f"rollback {service} → 世代 {source.id}",
                "services": services,
            }
        )
        (staging / GENERATION_FILE).write_text(
            payload.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (staging / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")
        final = self.root / f"{generation_id:04d}"
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
        return self.load(generation_id)

    def protected_ids(self, *, keep: int, in_use: Iterable[int] = ()) -> set[int]:
        """``gc`` 会保留的世代号。

        规则：最近 ``keep`` 个 + 当前 + **最新两个 stable** + **运行中进程仍在用的**。

        为什么要保两个 stable：自动回滚的目标是"上一个 stable"，只留一个的话
        ``gc`` 之后就没有回滚目标了——那等于把机制刚需的东西回收掉。

        为什么要保 ``in_use``：只重启变了服务意味着**进程会跨世代共存**，
        老进程的 ``BLUE_CONFIG_FILE`` 还指向它启动时那一代的目录。删掉那一代，
        服务只是靠内存里缓存的配置还活着，一旦重读配置就崩（实测踩过）。
        """
        generations = self.list()
        protected = {generation.id for generation in generations[-keep:]}
        current = self.current()
        if current is not None:
            protected.add(current.id)
        stables = [generation.id for generation in generations if generation.stable]
        protected.update(stables[-2:])
        protected.update(in_use)
        return protected

    def gc(
        self, *, keep: int, dry_run: bool = False, in_use: Iterable[int] = ()
    ) -> list[int]:
        """回收老世代：保留 :meth:`protected_ids` 算出来的那些。

        数据面永远不在这里被碰。

        :param in_use: 仍有存活进程在用的世代号（``gc`` 一律保留）
        :return: 命中的世代号列表
        """
        generations = self.list()
        if not generations:
            return []
        protected = self.protected_ids(keep=keep, in_use=in_use)
        removed: list[int] = []
        for generation in generations:
            if generation.id in protected:
                continue
            if not dry_run:
                shutil.rmtree(generation.path, ignore_errors=True)
            removed.append(generation.id)
        return removed

    def keep_hashes(
        self, *, keep: int, in_use: Iterable[int] = ()
    ) -> dict[str, set[str]]:
        """``gc`` 之后仍被引用的产物指纹：``{服务名: {artifact_hash}}``。"""
        generations = self.list()
        protected = self.protected_ids(keep=keep, in_use=in_use)
        keep_map: dict[str, set[str]] = {}
        for generation in generations:
            if generation.id not in protected:
                continue
            for name, entry in generation.services.items():
                if entry.artifact_hash:
                    keep_map.setdefault(name, set()).add(entry.artifact_hash)
        return keep_map

    def _load_path(self, path: Path) -> Generation | None:
        """从目录读世代；元数据损坏或结构不认识时返回 ``None``。"""
        meta_path = path / GENERATION_FILE
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        try:
            data = _GenerationFile.model_validate(raw)
        except ValidationError:
            return None
        return Generation(path=path, data=data)


def _rebind_hash(current: Generation, source: Generation, service: str) -> str:
    """单服务回滚后的合成内容指纹。

    它不等于任何 ``blue.infd`` 的编译结果——这是有意的：下一次 ``blue apply``
    会看到差异，从而把系统恢复成配置描述的样子。
    """
    fingerprint = "|".join(
        f"{name}:{entry.artifact_hash}:{entry.config_hash}"
        for name, entry in sorted(
            {**current.data.services, service: source.data.services[service]}.items()
        )
    )
    blob = f"{current.generation}|{source.generation}|{service}|{fingerprint}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> float:
    """当前时间戳。"""
    return time.time()
