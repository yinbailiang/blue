"""期望状态加载：编译 ``blue.infd``，合并服务元数据与契约元数据。

配置模型：

- ``blue.infd`` 顶层键 = 服务名，值是 ``ServiceTemplate(...)`` 的实例；
- ``supervisor`` 是唯一保留的非服务顶层键；
- 每个服务的元数据来自 ``services/<name>/service.infd`` 的 ``service`` 块；
- 每个契约包的元数据来自 ``contracts/<name>/contract.infd`` 的 ``contract`` 块。
- 每个 SDK 运行时包的元数据来自 ``sdk/<name>/sdk.infd`` 的 ``sdk`` 块。
- supervisor 把每个服务的运行时配置写入
  ``services/<name>/service_runtime_config.json``，文件内 ``config`` 段是该服务的
  业务配置，``infra`` 段是所有服务共享的基础设施参数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, cast

from infinity_data import CompilationResult, SandboxConfig, Severity, load
from infinity_data.emit import EmitConfig, to_json
from pydantic import ValidationError

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
from blue.errors import ConfigError
from blue.runtime import CONFIG_SECTION, INFRA_SECTION

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BUS_URL",
    "DEFAULT_CONFIG_PATH",
    "SECRET_FILES",
    "digest_package",
    "load_desired",
    "load_sdk",
    "load_supervisor",
    "read_package_name",
    "read_package_version",
    "redact_config",
    "redact_state",
    "remove_runtime_config",
    "render_state",
    "runtime_config_content",
    "scan_package",
    "topological_order",
    "write_runtime_configs",
]

DEFAULT_CONFIG_PATH = Path("blue.infd")
"""默认核心配置文件路径（相对当前工作目录）。"""

DEFAULT_BUS_URL = "amqp://guest:guest@127.0.0.1:5672/"
"""缺少 bus 服务时的总线默认地址。"""

_SECRET_HINTS = ("password", "api_key", "secret", "token", "credential")


def load_desired(path: str | Path = DEFAULT_CONFIG_PATH) -> DesiredState:
    """编译核心配置并加载全部服务元数据。

    :raises ConfigError: 文件缺失、编译失败、元数据非法或依赖图非法。
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")

    base_dir = config_path.parent
    result = _load_with_env(config_path, base_dir)
    if result.has_errors:
        raise ConfigError(_format_diagnostics(result.diagnostics, config_path))

    # 编译产物里可能是 Decimal / PurePosixPath（infd 的 ``p"..."``）。在入口就用
    # emit 投影成 JSON 原生值，下游（pydantic 校验、渲染、写盘、算指纹）只见一种形态。
    # 别在各处再手写 isinstance 兜底——``PurePosixPath`` 不是 ``Path`` 子类，极易漏。
    value = cast(dict[str, Any], json.loads(_project(result.value)))
    supervisor = load_supervisor(value.get("supervisor"), base_dir)
    contracts = _load_contracts(base_dir)

    services: dict[str, DesiredService] = {}
    for name, config in value.items():
        if name == "supervisor":
            continue
        if not isinstance(config, dict):
            raise ConfigError(
                f"服务 {name!r} 的配置必须是对象，实际为 {type(config).__name__}"
            )
        services[name] = _load_service(name, cast(dict[str, Any], config), base_dir)

    _validate_dependencies(services)
    _validate_contract_dependencies(services, contracts)
    sdk = load_sdk(base_dir)
    _validate_sdk_dependencies(services, sdk)
    bus_url, bus_transport = _bus_endpoint(services)
    deps_hash = _load_deps_hash(base_dir)
    return DesiredState(
        generation=_generation(supervisor, services, contracts, sdk, deps_hash),
        config_path=config_path,
        base_dir=base_dir,
        supervisor=supervisor,
        services=services,
        contracts=contracts,
        sdk=sdk,
        bus_url=bus_url,
        bus_transport=bus_transport,
        deps_hash=deps_hash,
    )


def topological_order(services: dict[str, DesiredService]) -> list[str]:
    """按依赖关系返回启动顺序；依赖在前。

    :raises ConfigError: 存在环或缺失依赖。
    """
    indegree = {name: 0 for name in services}
    dependents: dict[str, list[str]] = {name: [] for name in services}
    for name, service in services.items():
        for dependency in service.meta.depends_on:
            if dependency == name:
                raise ConfigError(f"服务 {name!r} 不能依赖自身")
            if dependency not in services:
                raise ConfigError(f"服务 {name!r} 依赖不存在的服务 {dependency!r}")
            indegree[name] += 1
            dependents[dependency].append(name)

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in sorted(dependents[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(services):
        cyclic = sorted(set(services) - set(order))
        raise ConfigError(f"服务依赖存在环：{', '.join(cyclic)}")
    return order


def runtime_config_content(service: DesiredService, desired: DesiredState) -> str:
    """生成服务的运行时配置文件内容：业务配置 + 共享基础设施参数。

    公开出来是为了可测：``infra`` 段的每个键都必须来自真实配置来源（supervisor 设置
    或服务元数据），写死常量会让对应配置项永远改不动——``idempotency`` 就这么死过一次。
    """
    settings = desired.supervisor
    payload = {
        CONFIG_SECTION: service.config,
        INFRA_SECTION: {
            "project_root": str(desired.base_dir),
            "bus_url": desired.bus_url,
            "state_dir": str(settings.state_dir),
            "contracts": {
                name: desired.contracts[name].meta.module
                for name in sorted(service.meta.depends_contracts)
                if name in desired.contracts
            },
            "registry_ttl": settings.registry_ttl,
            "registry_sweep_interval": settings.registry_sweep_interval,
            # 服务据此把自己的停机排空超时压在这个预算内——supervisor 到点就 SIGKILL，
            # 排空超时若比它大，已投递未派发的消息会被 broker 重投（at-least-once）。
            "stop_timeout": service.meta.stop_timeout,
            # 事件去重（业务不必自己写幂等）；去重库落在 state_dir/dedupe 下。
            # 由 supervisor 配置下发，别在这里写死——写死等于这项配置永远改不动。
            "idempotency": settings.idempotency,
            "idempotency_retention": settings.idempotency_retention,
            "idempotency_sweep_interval": settings.idempotency_sweep_interval,
        },
    }
    return (
        to_json(
            payload,
            config=EmitConfig(indent=2, sort_keys=True, ensure_ascii=False),
        )
        + "\n"
    )


def write_runtime_configs(
    desired: DesiredState, *, only_changed: bool = True
) -> list[str]:
    """把运行时配置写入每个服务目录下的 ``service_runtime_config.json``。

    单个文件里分成两段：``config`` 是该服务的 ``ServiceTemplate`` 实例，
    ``infra`` 是所有服务共享的基础设施参数。服务只需定位一份配置。

    :return: 实际被写入（内容或权限发生变化）的服务名列表。
    """
    changed: list[str] = []
    for name, service in desired.services.items():
        content = runtime_config_content(service, desired)
        path = service.config_path
        if only_changed and path.is_file():
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = ""
            if current == content:
                _chmod_private(path)
                continue
        _atomic_write(path, content)
        changed.append(name)
    return changed


def remove_runtime_config(service: DesiredService) -> None:
    """删除 service 已生成的运行时配置（服务被移出期望状态时调用）。"""
    service.config_path.unlink(missing_ok=True)


def read_package_version(pyproject: Path) -> str:
    """读取 ``pyproject.toml`` 中声明的包版本。

    文件缺失、无法解析或未声明 ``project.version`` 时返回空串——版本只用于
    变更检测，读不到不应阻断启动。
    """
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError, tomllib.TOMLDecodeError:
        return ""
    project = data.get("project")
    if not isinstance(project, dict):
        return ""
    version = cast(dict[str, Any], project).get("version")
    return version if isinstance(version, str) else ""


def read_package_name(pyproject: Path) -> str:
    """读取 ``pyproject.toml`` 中声明的包名。"""
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError, tomllib.TOMLDecodeError:
        return ""
    project = data.get("project")
    if not isinstance(project, dict):
        return ""
    name = cast(dict[str, Any], project).get("name")
    return name if isinstance(name, str) else ""


_JUNK_DIRS = frozenset(
    {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
"""与产物无关、不该进指纹的目录名（出现在 ``src/`` 下任意层级）。"""

_JUNK_SUFFIXES = (".pyc", ".pyo")
"""字节码文件；解释器会在运行时生成它们，与源码无关。"""

_JUNK_NAMES = frozenset({".DS_Store"})
"""编辑器/系统产生的孤立文件。"""


def _is_junk(path: Path, root: Path) -> bool:
    """判断 ``root`` 下的文件是否与打出来的 wheel 内容无关。

    缓存目录会在正常使用中反复变化：把它们算进指纹，等于每次跑完测试都
    触发一次无意义的重建/重启。
    """
    if path.suffix in _JUNK_SUFFIXES or path.name in _JUNK_NAMES:
        return True
    return any(part in _JUNK_DIRS for part in path.relative_to(root).parts[:-1])


def scan_package(
    service_dir: Path, *, extra_files: Iterable[str] = ()
) -> tuple[tuple[str, int, int], ...]:
    """列出参与源码指纹的文件：``pyproject.toml``、额外文件与 ``src/`` 全部内容。

    ``src/`` 下**不限扩展名**：包里的非 Python 资产（例如 ``webui`` 的
    ``web/**`` 静态资源）会被打进 wheel，漏掉它们就会出现「改了前端资源、
    指纹不变、apply 认为无事可做」——资源永远不生效。

    只做 ``stat``，供调谐循环低成本判断「可能变了」；确认内容是否真的变化
    交给 :func:`digest_package`。
    """
    entries: list[tuple[str, int, int]] = []
    pyproject = service_dir / "pyproject.toml"
    try:
        stat = pyproject.stat()
    except OSError:
        pass
    else:
        entries.append(("pyproject.toml", stat.st_size, stat.st_mtime_ns))

    for name in extra_files:
        path = service_dir / name
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((name, stat.st_size, stat.st_mtime_ns))

    src = service_dir / "src"
    if not src.is_dir():
        return tuple(entries)
    paths = sorted(
        (path for path in src.rglob("*") if path.is_file() and not _is_junk(path, src)),
        key=lambda item: item.as_posix(),
    )
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            (path.relative_to(service_dir).as_posix(), stat.st_size, stat.st_mtime_ns)
        )
    return tuple(entries)


def digest_package(
    service_dir: Path, entries: tuple[tuple[str, int, int], ...] | None = None
) -> str:
    """计算服务包内容指纹。

    覆盖 ``pyproject.toml``（含版本声明）与 ``src/`` 下全部产物文件的内容，
    因此「只改代码、不升版本」与「只改包内静态资源」同样会改变指纹——指纹
    必须与打进 wheel 的文件集合一致，否则内容寻址会复用旧产物。
    ``entries`` 可由 :func:`scan_package` 预先提供，避免重复遍历目录。
    """
    digest = hashlib.sha256()
    items = scan_package(service_dir) if entries is None else entries
    for name, _size, _mtime in items:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update((service_dir / name).read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def render_state(desired: DesiredState, *, redact: bool = True) -> dict[str, Any]:
    """把期望状态渲染成可打印的 JSON 结构；默认脱敏。

    模板引擎用 ``Decimal`` 表示 infd 里的浮点字面量、用 ``Path`` 表示路径，它们
    都不是 JSON 原生类型。这里统一就地转换，返回的结构可以直接交给
    ``json.dumps``——否则 ``blue render`` 会在序列化那一刻抛 ``TypeError``。

    :param desired: 编译好的期望状态。
    :param redact: 是否把疑似密钥的字段换成 ``***``。
    :return: 可被 ``json.dumps`` 直接序列化的嵌套结构。
    """
    services: dict[str, Any] = {}
    for name, service in desired.services.items():
        config = _redact(service.config) if redact else service.config
        meta = service.meta.model_dump(mode="json")
        if redact and meta.get("env"):
            # ``env`` 的**键名不可信**（``FOO=secret`` 完全合法），命不中
            # _SECRET_HINTS，所以整块值一律打码、只留键名便于排错：
            # ``blue render`` 默认脱敏，将来真把密钥写进 meta.env 也不会明文输出。
            meta["env"] = {key: "***" for key in cast(dict[str, str], meta["env"])}
        services[name] = {
            "meta": meta,
            "config": _json_safe(config),
            "config_hash": service.config_hash,
            "package_version": service.package_version,
            "source_hash": service.source_hash,
            "config_path": str(service.config_path),
        }
    contracts: dict[str, Any] = {}
    for name, contract in desired.contracts.items():
        contracts[name] = {
            "meta": contract.meta.model_dump(mode="json"),
            "package_version": contract.package_version,
            "source_hash": contract.source_hash,
            "contract_path": str(contract.contract_path),
        }
    return {
        "generation": desired.generation,
        "config_path": str(desired.config_path),
        "base_dir": str(desired.base_dir),
        "supervisor": desired.supervisor.model_dump(mode="json"),
        "bus_url": desired.bus_url,
        "bus_transport": desired.bus_transport,
        "contracts": contracts,
        "sdk": desired.sdk.model_dump(mode="json"),
        "services": services,
    }


def redact_state(desired: DesiredState) -> dict[str, Any]:
    """渲染并脱敏期望状态。"""
    return render_state(desired, redact=True)


def load_supervisor(raw: object, base_dir: Path) -> SupervisorSettings:
    """解析 ``supervisor`` 块；未知键只告警、不报错。

    ``blue.infd`` 是由**已经在跑的那个**调谐器解析的。如果新增配置项一律拒绝
    （``extra="forbid"``），升级路径就会死锁：旧代码认不出新键 → 热重载失败；
    又因为"配置编译失败"放弃 execv 自重启 → 永远停在 old generation 上，只能
    `blue down && blue up` 人工救。实测就是这么卡住的。

    所以未知键放行（``extra="allow"``）并**告警点名**——代价是拼错的键不会报错，
    用告警兜住。
    """
    if raw is None:
        settings = SupervisorSettings()
    elif isinstance(raw, dict):
        try:
            settings = SupervisorSettings.model_validate(raw)
        except ValidationError as error:
            raise ConfigError(f"supervisor 配置校验失败：{error}") from error
        unknown = sorted(settings.model_extra or {})
        if unknown:
            logger.warning(
                "supervisor 块里有本版本不认识的键（拼错了？还是配置比代码新？）：%s",
                ", ".join(unknown),
            )
    else:
        raise ConfigError("supervisor 必须是对象")

    state_dir = settings.state_dir
    if not state_dir.is_absolute():
        state_dir = (base_dir / state_dir).resolve()
    return settings.model_copy(update={"state_dir": state_dir})


def _load_contracts(base_dir: Path) -> dict[str, DesiredContract]:
    """加载 ``contracts/*/contract.infd`` 并计算源码指纹。"""
    root = (base_dir / "contracts").resolve()
    if not root.is_dir():
        return {}

    contracts: dict[str, DesiredContract] = {}
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        contract_file = directory / "contract.infd"
        if not contract_file.is_file():
            raise ConfigError(
                f"契约目录 {directory.name!r} 缺少元数据文件：{contract_file}"
            )

        result = _load_with_env(contract_file, base_dir)
        if result.has_errors:
            raise ConfigError(_format_diagnostics(result.diagnostics, contract_file))
        raw_meta = result.value.get("contract")
        if not isinstance(raw_meta, dict):
            raise ConfigError(f"{contract_file} 缺少 contract 元数据块")
        try:
            meta = ContractMeta.model_validate(raw_meta)
        except ValidationError as error:
            raise ConfigError(
                f"契约 {directory.name!r} 的 contract 元数据校验失败：{error}"
            ) from error

        if meta.name != directory.name:
            raise ConfigError(
                f"契约名不一致：目录为 {directory.name!r}，"
                f"{contract_file} 中 contract.name 为 {meta.name!r}"
            )

        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            raise ConfigError(f"契约 {meta.name!r} 缺少 pyproject.toml")
        actual_package = read_package_name(pyproject)
        if actual_package and actual_package != meta.package:
            raise ConfigError(
                f"契约 {meta.name!r} 包名不一致：contract.package 为 "
                f"{meta.package!r}，pyproject.toml 为 {actual_package!r}"
            )

        entries = scan_package(directory, extra_files=("contract.infd",))
        contracts[meta.name] = DesiredContract(
            meta=meta,
            package_version=read_package_version(pyproject),
            source_hash=digest_package(directory, entries),
            contract_dir=directory,
            contract_path=contract_file,
        )
    return contracts


def _load_deps_hash(base_dir: Path) -> str:
    """计算根 ``uv.lock`` 的内容指纹。

    依赖变更（加包、升版本）不改变任何源码指纹，但**会**改变产物内容，所以要进
    ``generation``——否则 apply 认为无事可做，产物永远不会重建。锁文件缺失
    （例如最小化测试工作区）时返回空串，不阻断编译。
    """
    try:
        return hashlib.sha256((base_dir / "uv.lock").read_bytes()).hexdigest()
    except OSError:
        return ""


def load_sdk(base_dir: Path) -> DesiredSdk:
    """计算 blue 核心与 ``sdk/*`` 运行时包的指纹。

    核心的 ``source_hash`` 只覆盖 ``src/blue/**`` + 根 pyproject；``sdk/<name>/``
    各自用 ``sdk.infd`` 声明元数据（与 ``contract.infd`` 同构），按 **SDK 名**
    记录在 ``packages`` 里，供 ``depends_sdk`` 精准重启与产物安装使用。

    注意：**不能**用 :func:`scan_package` 扫核心——那套是"服务包布局"
    （``pyproject.toml`` + 内部 ``src/``），而 SDK 目录本身就在 ``src/blue`` 下，
    套进去会一个文件都扫不到（曾经就是这样：指纹只覆盖了根
    ``pyproject.toml``，改代码全都不算变化）。``sdk/*`` 是标准包布局，可以用。
    """
    sdk_dir = base_dir / "src" / "blue"
    packages_root = (base_dir / "sdk").resolve()
    if not sdk_dir.is_dir() and not packages_root.is_dir():
        return DesiredSdk()
    root_pyproject = base_dir / "pyproject.toml"
    digest = hashlib.sha256()
    if root_pyproject.is_file():
        digest.update(b"pyproject.toml\0")
        digest.update(root_pyproject.read_bytes())
        digest.update(b"\0")
    if sdk_dir.is_dir():
        for path in sorted(sdk_dir.rglob("*.py"), key=lambda item: item.as_posix()):
            digest.update(path.relative_to(sdk_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<missing>")
            digest.update(b"\0")

    packages: dict[str, DesiredSdkPackage] = {}
    if packages_root.is_dir():
        for directory in sorted(packages_root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            manifest = directory / "sdk.infd"
            if not manifest.is_file():
                raise ConfigError(
                    f"SDK 目录 {directory.name!r} 缺少元数据文件：{manifest}"
                )
            result = _load_with_env(manifest, base_dir)
            if result.has_errors:
                raise ConfigError(_format_diagnostics(result.diagnostics, manifest))
            raw_meta = result.value.get("sdk")
            if not isinstance(raw_meta, dict):
                raise ConfigError(f"{manifest} 缺少 sdk 元数据块")
            try:
                meta = SdkMeta.model_validate(raw_meta)
            except ValidationError as error:
                raise ConfigError(
                    f"SDK {directory.name!r} 的 sdk 元数据校验失败：{error}"
                ) from error
            if meta.name != directory.name:
                raise ConfigError(
                    f"SDK 名不一致：目录为 {directory.name!r}，"
                    f"{manifest} 中 sdk.name 为 {meta.name!r}"
                )
            pyproject = directory / "pyproject.toml"
            if not pyproject.is_file():
                raise ConfigError(f"SDK {meta.name!r} 缺少 pyproject.toml")
            actual_package = read_package_name(pyproject)
            if actual_package and actual_package != meta.package:
                raise ConfigError(
                    f"SDK {meta.name!r} 包名不一致：sdk.package 为 "
                    f"{meta.package!r}，pyproject.toml 为 {actual_package!r}"
                )
            entries = scan_package(directory, extra_files=("sdk.infd",))
            packages[meta.name] = DesiredSdkPackage(
                meta=meta,
                package_version=read_package_version(pyproject),
                source_hash=digest_package(directory, entries),
            )

    return DesiredSdk(
        package_version=read_package_version(root_pyproject),
        source_hash=digest.hexdigest(),
        packages=packages,
    )


def _load_service(name: str, config: dict[str, Any], base_dir: Path) -> DesiredService:
    service_dir = (base_dir / "services" / name).resolve()
    service_file = service_dir / "service.infd"
    if not service_file.is_file():
        raise ConfigError(f"服务 {name!r} 缺少元数据文件：{service_file}")

    result = _load_with_env(service_file, base_dir)
    if result.has_errors:
        raise ConfigError(_format_diagnostics(result.diagnostics, service_file))
    service_value = result.value
    raw_meta = service_value.get("service")
    if not isinstance(raw_meta, dict):
        raise ConfigError(f"{service_file} 缺少 service 元数据块")
    try:
        meta = ServiceMeta.model_validate(raw_meta)
    except ValidationError as error:
        raise ConfigError(
            f"服务 {name!r} 的 service 元数据校验失败：{error}"
        ) from error

    if meta.name != name:
        raise ConfigError(
            f"服务名不一致：blue.infd 顶层键为 {name!r}，"
            f"{service_file} 中 service.name 为 {meta.name!r}"
        )

    entries = scan_package(service_dir)
    return DesiredService(
        meta=meta,
        config=config,
        config_hash=_hash_json(config),
        package_version=read_package_version(service_dir / "pyproject.toml"),
        source_hash=digest_package(service_dir, entries),
        service_dir=service_dir,
        config_path=service_dir / "service_runtime_config.json",
    )


def _validate_dependencies(services: dict[str, DesiredService]) -> None:
    # topological_order 同时完成缺失依赖、自依赖与环检测
    topological_order(services)


def _validate_contract_dependencies(
    services: dict[str, DesiredService], contracts: dict[str, DesiredContract]
) -> None:
    for name, service in services.items():
        seen: set[str] = set()
        for contract in service.meta.depends_contracts:
            if contract in seen:
                raise ConfigError(f"服务 {name!r} 重复声明契约依赖 {contract!r}")
            seen.add(contract)
            if contract not in contracts:
                raise ConfigError(f"服务 {name!r} 依赖不存在的契约 {contract!r}")


def _validate_sdk_dependencies(
    services: dict[str, DesiredService], sdk: DesiredSdk
) -> None:
    for name, service in services.items():
        seen: set[str] = set()
        for dependency in service.meta.depends_sdk:
            if dependency in seen:
                raise ConfigError(f"服务 {name!r} 重复声明 SDK 依赖 {dependency!r}")
            seen.add(dependency)
            if dependency not in sdk.packages:
                raise ConfigError(f"服务 {name!r} 依赖不存在的 SDK {dependency!r}")


def _bus_endpoint(
    services: dict[str, DesiredService],
) -> tuple[str, Literal["rabbit", "memory"]]:
    bus = services.get("bus")
    if bus is None:
        return os.environ.get("BLUE_BUS_URL", DEFAULT_BUS_URL), "rabbit"

    config = bus.config
    proxy = bool(config.get("proxy", False))
    proxy_listen = config.get("proxy_listen")
    url = config.get("url")
    if proxy and isinstance(proxy_listen, str) and proxy_listen:
        endpoint = proxy_listen
    elif isinstance(url, str) and url:
        endpoint = url
    else:
        endpoint = DEFAULT_BUS_URL

    transport = config.get("transport", "rabbit")
    if transport not in ("rabbit", "memory"):
        transport = "rabbit"
    return endpoint, transport


def _generation(
    supervisor: SupervisorSettings,
    services: dict[str, DesiredService],
    contracts: dict[str, DesiredContract],
    sdk: DesiredSdk,
    deps_hash: str = "",
) -> str:
    payload = {
        "supervisor": supervisor.model_dump(mode="json"),
        "sdk": sdk.model_dump(mode="json"),
        "deps_hash": deps_hash,
        "contracts": {
            name: {
                "meta": contract.meta.model_dump(mode="json"),
                "package_version": contract.package_version,
                "source_hash": contract.source_hash,
            }
            for name, contract in sorted(contracts.items())
        },
        "services": {
            name: {
                "meta": service.meta.model_dump(mode="json"),
                "config_hash": service.config_hash,
                "package_version": service.package_version,
                "source_hash": service.source_hash,
            }
            for name, service in sorted(services.items())
        },
    }
    return _hash_json(payload)


def _load_with_env(path: Path, workspace_root: Path) -> CompilationResult:
    """编译配置，并按需授权 ``!env`` 变量、``services/**`` 与 ``contracts/**``。"""
    patterns = _sandbox_patterns(workspace_root)
    strict = load(
        path,
        sandbox=SandboxConfig(
            allow_env=[],
            allow_templates=patterns,
            allow_files=patterns,
        ),
    )
    names = _requested_env_names(strict.diagnostics)
    if not names:
        return strict
    return load(
        path,
        sandbox=SandboxConfig(
            allow_env=names,
            allow_templates=patterns,
            allow_files=patterns,
        ),
    )


SECRET_FILES = ("secrets.toml", "secrets.json")
"""允许被 ``!file`` 导入的密钥文件名。

只授权这两个具体文件，而不是整个项目根目录——密钥文件是配置里唯一需要
从工作区读取的外部数据。
"""


def _sandbox_patterns(workspace_root: Path) -> list[str]:
    services = (workspace_root / "services").resolve()
    contracts = (workspace_root / "contracts").resolve()
    sdk = (workspace_root / "sdk").resolve()
    patterns = [
        str(services / "**"),
        "services/**",
        str(contracts / "**"),
        "contracts/**",
        str(sdk / "**"),
        "sdk/**",
    ]
    for name in SECRET_FILES:
        patterns.append(str(workspace_root / name))
        patterns.append(name)
    return patterns


def _requested_env_names(diagnostics: list[Any]) -> list[str]:
    names = {
        str(item.params["name"])
        for item in diagnostics
        if item.code == "sandbox.env_unauthorized" and "name" in item.params
    }
    return sorted(names)


def _format_diagnostics(diagnostics: list[Any], path: Path) -> str:
    errors = [item for item in diagnostics if item.severity is Severity.ERROR]
    lines = [f"  - {item.location} [{item.code}] {item.message}" for item in errors]
    return f"配置编译失败：{path}\n" + "\n".join(lines)


_COMPACT_EMIT = EmitConfig(indent=None, sort_keys=True, ensure_ascii=False)
"""指纹用：紧凑、排序、不转义（同一份配置必须逐字节稳定）。"""


def _project(value: object) -> str:
    """把配置值投影成 JSON 文本。

    ``path → POSIX 字符串``、``Decimal → 数字`` 都是 emit 层的职责
    （``EmitConfig.full_path / full_float`` 默认即有损投影），这里不自己写转换。

    注意投影是**有损**的：``Decimal("2.0")`` 落成 ``2``、``PurePosixPath`` 落成字符串。
    所以任何基于投影的哈希（见 :func:`_hash_json`）都绑定了当前 emit 语义——改投影
    等于改指纹，见该函数的说明。
    """
    return to_json(value, config=_COMPACT_EMIT)


def _hash_json(value: object) -> str:
    """对 JSON 原生值取稳定指纹。

    ``value`` 必须已经过 :func:`_project`（:func:`load_desired` 在入口统一投影一次），
    否则 ``Decimal``/``Path`` 会直接抛 ``TypeError``。

    **投影语义不是"随便换个等价实现都零成本"**：e365780 之前是 ``Decimal → float``
    （``Decimal("2.0")`` → ``2.0``），现在走 emit（整值落成 ``2``），canonical JSON
    不同 → ``config_hash`` 变 → ``generation`` 变。那次实测让 10 个服务的指纹变了，
    只是当时 state.json 已经按新代码写过一次，所以事后看不到差异。以后再动投影层，
    请预期一次全量重启，别按"逐字节一致"来假设。
    """
    projected = json.loads(_project(value))
    canonical = json.dumps(
        projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _chmod_private(path: Path) -> None:
    try:
        if path.stat().st_mode & 0o777 != 0o600:
            path.chmod(0o600)
    except OSError:
        return


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """把配置里的密钥替换成 ``***``，用于写入状态快照。

    状态文件本身是 0600，但"能不落盘就不落盘"：``blue plan --diff`` 只需要
    知道字段变没变，不需要看到密钥内容。
    """
    redacted = _redact(config)
    return cast(dict[str, Any], redacted) if isinstance(redacted, dict) else {}


def _json_safe(value: object) -> object:
    """把配置值投影成 JSON 原生类型（供 ``render_state`` 打印）。

    转换语义交给 emit 层（见 :func:`_project`）：``path → POSIX 字符串``、
    ``Decimal → 数字``、元组 → 列表。这里不再自己写 isinstance 链——之前手写时
    漏了「``PurePosixPath`` 不是 ``Path`` 子类」这件事。
    """
    return json.loads(_project(value))


def _redact(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            key_text = str(key)
            if any(hint in key_text.lower() for hint in _SECRET_HINTS):
                result[key_text] = "***"
            else:
                result[key_text] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in cast(list[object], value)]
    return value
