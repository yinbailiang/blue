"""blue bus 服务：管理 RabbitMQ 生命周期、配置与可选 TCP 代理。"""

from __future__ import annotations

from blue_service_bus.config import BusConfig
from blue_service_bus.service import BusService

__all__ = ["BusConfig", "BusService", "main"]

CONFIG_MODEL = BusConfig
"""契约测试使用的运行时配置模型。"""


def main() -> int:
    """控制台脚本入口（延迟导入）。"""
    from blue_service_bus.__main__ import main as _main

    return _main()
