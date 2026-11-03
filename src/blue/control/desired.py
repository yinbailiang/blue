"""期望状态加载：编译 ``blue.infd``，合并服务元数据与配置实例。

配置模型：

- ``blue.infd`` 顶层键 = 服务名，值是 ``ServiceTemplate(...)`` 的实例；
- ``supervisor`` 是唯一保留的非服务顶层键；
- 每个服务的元数据来自 ``services/<name>/service.infd`` 的 ``service`` 块；
- supervisor 把服务配置写入 ``services/<name>/service_runtime_config.json``。
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from infinity_data import CompilationResult, SandboxConfig, Severity, load
from pydantic import ValidationError

from blue.control.model import (
    DesiredService,
    DesiredState,
    ServiceMeta,
    SupervisorSettings,
)
from blue.errors import ConfigError

__all__ = [
    "DEFAULT_BUS_URL",
    "DEFAULT_CONFIG_PATH",
    "digest_package",
    "load_desired",
    "read_package_version",
    "redact_state",
    "remove_runtime_config",
    "render_state",
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

    value = result.value
    supervisor = _load_supervisor(value.get("supervisor"), base_dir)

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
    bus_url, bus_transport = _bus_endpoint(services)
    return DesiredState(
        generation=_generation(supervisor, services),
        config_path=config_path,
        base_dir=base_dir,
        supervisor=supervisor,
        services=services,
        bus_url=bus_url,
        bus_transport=bus_transport,
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


def write_runtime_configs(
    desired: DesiredState, *, only_changed: bool = True
) -> list[str]:
    """把每个服务的配置实例写入其目录下的 ``service_runtime_config.json``。

    :return: 实际被写入（内容或权限发生变化）的服务名列表。
    """
    changed: list[str] = []
    for name, service in desired.services.items():
        content = (
            json.dumps(
                service.config,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
            + "\n"
        )
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


def scan_package(service_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """列出参与源码指纹的文件：``pyproject.toml`` 与 ``src/**/*.py``。

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

    src = service_dir / "src"
    if not src.is_dir():
        return tuple(entries)
    paths = sorted(
        (path for path in src.rglob("*.py") if path.is_file()),
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

    覆盖 ``pyproject.toml``（含版本声明）与全部 ``src/**/*.py`` 的内容，因此
    「只改代码、不升版本」同样会改变指纹。``entries`` 可由
    :func:`scan_package` 预先提供，避免重复遍历目录。
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
    """把期望状态渲染成可打印的 JSON 结构；默认脱敏。"""
    services: dict[str, Any] = {}
    for name, service in desired.services.items():
        config = _redact(service.config) if redact else service.config
        services[name] = {
            "meta": service.meta.model_dump(mode="json"),
            "config": config,
            "config_hash": service.config_hash,
            "package_version": service.package_version,
            "source_hash": service.source_hash,
            "config_path": str(service.config_path),
        }
    return {
        "generation": desired.generation,
        "config_path": str(desired.config_path),
        "base_dir": str(desired.base_dir),
        "supervisor": desired.supervisor.model_dump(mode="json"),
        "bus_url": desired.bus_url,
        "bus_transport": desired.bus_transport,
        "services": services,
    }


def redact_state(desired: DesiredState) -> dict[str, Any]:
    """渲染并脱敏期望状态。"""
    return render_state(desired, redact=True)


def _load_supervisor(raw: object, base_dir: Path) -> SupervisorSettings:
    if raw is None:
        settings = SupervisorSettings()
    elif isinstance(raw, dict):
        try:
            settings = SupervisorSettings.model_validate(raw)
        except ValidationError as error:
            raise ConfigError(f"supervisor 配置校验失败：{error}") from error
    else:
        raise ConfigError("supervisor 必须是对象")

    state_dir = settings.state_dir
    if not state_dir.is_absolute():
        state_dir = (base_dir / state_dir).resolve()
    return settings.model_copy(update={"state_dir": state_dir})


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
    supervisor: SupervisorSettings, services: dict[str, DesiredService]
) -> str:
    payload = {
        "supervisor": supervisor.model_dump(mode="json"),
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
    """编译配置，并按需授权 ``!env`` 变量与 ``services/**`` 模板。"""
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


def _sandbox_patterns(workspace_root: Path) -> list[str]:
    services = (workspace_root / "services").resolve()
    return [str(services / "**"), "services/**"]


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


def _hash_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"无法序列化类型：{type(value).__name__}")


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
