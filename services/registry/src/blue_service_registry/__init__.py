"""工具运行时注册中心。"""

from blue_service_registry.config import RegistryConfig
from blue_service_registry.registry import (
    ToolRegistryService,
    ToolRegistryState,
    WorkerRecord,
)
from blue_service_registry.subscribers.registry import RegistryBackend, RegistryHandler

__all__ = [
    "RegistryBackend",
    "RegistryConfig",
    "RegistryHandler",
    "ToolRegistryService",
    "ToolRegistryState",
    "WorkerRecord",
    "main",
]


def main() -> int:
    """控制台脚本入口（延迟导入，避免包导入即启动）。"""
    from blue_service_registry.__main__ import main as _main

    return _main()
