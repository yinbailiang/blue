"""blue agent 服务包。"""

from __future__ import annotations

from blue_service_agent.aggregate import AgentAggregate
from blue_service_agent.config import AgentConfig, RegistryConfig

__all__ = ["AgentAggregate", "AgentConfig", "RegistryConfig", "main"]

CONFIG_MODEL = AgentConfig
"""契约测试使用的运行时配置模型。"""


def main() -> int:
    """控制台脚本入口（延迟导入，避免包导入即启动）。"""
    from blue_service_agent.__main__ import main as _main

    return _main()
