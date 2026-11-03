"""服务侧运行时辅助：读运行时配置、连总线、把工具/服务跑到退出。

每个外置服务只依赖本模块与 ``blue`` SDK，不读取核心 ``blue.infd``。
supervisor 通过环境变量注入连接信息，通过 ``service_runtime_config.json``
注入服务配置。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from blue_contracts_tool import EVENTS as TOOL_EVENTS
from infinity_bus import EventDeclaration
from pydantic import BaseModel

from blue.errors import RuntimeConfigError
from blue.lifecycle import LifecycleService, serve_forever
from blue.transport import RabbitRuntime
from blue.worker import ToolWorker

__all__ = [
    "DEFAULT_BUS_URL",
    "DEFAULT_RUNTIME_CONFIG",
    "build_runtime",
    "bus_url",
    "load_runtime_config",
    "registry_ttl",
    "run_service",
    "run_tool",
    "runtime_config_path",
    "service_name",
]

DEFAULT_RUNTIME_CONFIG = "service_runtime_config.json"
"""supervisor 生成并放在服务目录下的运行时配置文件名。"""

DEFAULT_BUS_URL = "amqp://guest:guest@127.0.0.1:5672/"
"""未注入 ``BLUE_BUS_URL`` 时的开发默认值。"""

ConfigT = TypeVar("ConfigT", bound=BaseModel)


def runtime_config_path(path: str | Path | None = None) -> Path:
    """定位运行时配置：显式参数 > ``BLUE_CONFIG_FILE`` > 当前目录默认文件。"""
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("BLUE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_CONFIG).resolve()


def load_runtime_config[ConfigT: BaseModel](
    model: type[ConfigT], path: str | Path | None = None
) -> ConfigT:
    """读取并校验运行时配置。

    :raises RuntimeConfigError: 文件不存在、不可读或字段校验失败。
    """
    target = runtime_config_path(path)
    if not target.is_file():
        raise RuntimeConfigError(f"运行时配置不存在：{target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeConfigError(f"无法读取运行时配置：{target}：{error}") from error
    try:
        return model.model_validate_json(text)
    except Exception as error:
        raise RuntimeConfigError(f"运行时配置校验失败：{target}：{error}") from error


def bus_url() -> str:
    """取总线地址（supervisor 注入 ``BLUE_BUS_URL``）。"""
    return os.environ.get("BLUE_BUS_URL", DEFAULT_BUS_URL)


def service_name(default: str = "service") -> str:
    """取当前服务名（supervisor 注入 ``BLUE_SERVICE_NAME``）。"""
    return os.environ.get("BLUE_SERVICE_NAME", default)


def registry_ttl(default: float = 15.0) -> float:
    """工具租约时长（秒）：读 supervisor 注入的 ``BLUE_REGISTRY_TTL``。

    环境变量缺失、无法解析或非正数时回退 ``default``，避免把一个坏值变成
    「立刻过期」的租约。
    """
    raw = os.environ.get("BLUE_REGISTRY_TTL")
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def build_runtime(
    role: str,
    events: Iterable[type[EventDeclaration]],
    url: str | None = None,
) -> RabbitRuntime:
    """创建一个 RabbitMQ 总线成员。

    成员 ID 优先使用 supervisor 注入的 ``BLUE_INSTANCE_ID``，保证重启后
    身份可追踪；手工运行时退化为随机短 ID。

    ``events`` 是该成员需要注册的事件声明；每个进程只注册自己生产/消费的事件。
    """
    instance = os.environ.get("BLUE_INSTANCE_ID") or uuid.uuid4().hex[:8]
    return RabbitRuntime(
        url=url or bus_url(),
        member_id=f"{role}.{instance}",
        events=events,
    )


def run_service(service: LifecycleService) -> int:
    """把生命周期服务跑到收到 SIGINT / SIGTERM。"""
    asyncio.run(serve_forever(service))
    return 0


def run_tool(
    tool: Callable[..., Any],
    name: str,
    *,
    ttl: float | None = None,
) -> int:
    """把工具函数作为独立进程运行。

    :param tool: 带完整类型签名的工具函数。
    :param name: 工具名（用于成员命名与日志）。
    :param ttl: 注册租约；缺省读 ``BLUE_REGISTRY_TTL``，再缺省 15 秒。
    """
    lease = ttl if ttl is not None else registry_ttl()
    worker = ToolWorker(
        runtime=build_runtime(f"tool.{name}", TOOL_EVENTS),
        tool=tool,
        ttl=lease,
    )
    return run_service(worker)
