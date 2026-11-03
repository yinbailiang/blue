"""外置程序的公共设施：读配置、建立总线成员、把服务跑到退出。

外置程序只依赖 ``blue`` 包对外暴露的契约（配置模型、协议、传输、生命周期），
不经过核心命令行启动。
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from blue.config import DEFAULT_CONFIG_PATH, BlueSettings, ConfigError, load_settings
from blue.lifecycle import LifecycleService, serve_forever
from blue.transport import BusRuntime, RabbitRuntime
from blue.worker import ToolWorker

__all__ = [
    "build_runtime",
    "load_settings_for_external",
    "run_service",
    "run_tool",
]


def load_settings_for_external(config_path: str | None = None) -> BlueSettings:
    """读取配置；外置程序只支持跨进程传输。

    :raises ConfigError: 配置不可用或使用了仅限进程内的 ``memory`` 传输。
    """
    settings = load_settings(config_path or DEFAULT_CONFIG_PATH)
    if settings.bus.transport != "rabbit":
        raise ConfigError("外置程序需要 rabbit 传输；memory 传输仅用于进程内测试")
    return settings


def build_runtime(settings: BlueSettings, role: str) -> BusRuntime:
    """创建一个独立的总线成员（每个外置程序一个成员）。"""
    return RabbitRuntime(
        url=settings.bus.url,
        member_id=f"{role}.{uuid.uuid4().hex[:8]}",
    )


def run_service(service: LifecycleService) -> int:
    """运行服务直到收到 SIGINT / SIGTERM。"""
    asyncio.run(serve_forever(service))
    return 0


def run_tool(
    tool: Callable[..., Any],
    name: str,
    argv: Sequence[str] | None = None,
) -> int:
    """把一个工具函数作为独立进程运行。

    :param tool: 工具函数（需完整类型签名）。
    :param name: 工具名，用于命令行提示与成员命名。
    :param argv: 命令行参数（缺省取 ``sys.argv``）。
    """
    parser = argparse.ArgumentParser(
        prog=f"python -m handlers.tools.{name}",
        description=tool.__doc__ or f"{name} 工具进程",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径"
    )
    args = parser.parse_args(argv)

    settings = load_settings_for_external(args.config)
    worker = ToolWorker(
        runtime=build_runtime(settings, f"tool.{name}"),
        tool=tool,
        ttl=settings.registry.ttl,
    )
    return run_service(worker)
