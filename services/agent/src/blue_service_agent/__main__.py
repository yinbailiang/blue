"""blue agent 服务入口：读取运行时配置并在总线上运行模型核心 + 工具目录。"""

from __future__ import annotations

import os

from blue.runtime import build_runtime, load_runtime_config, run_service
from blue_service_agent.aggregate import AgentAggregate
from blue_service_agent.config import AgentConfig, RegistryConfig

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    config = load_runtime_config(AgentConfig)
    registry = RegistryConfig(
        ttl=float(os.environ.get("BLUE_REGISTRY_TTL", "15")),
        sweep_interval=float(os.environ.get("BLUE_REGISTRY_SWEEP_INTERVAL", "2")),
    )
    aggregate = AgentAggregate(
        runtime=build_runtime("agent"),
        agent=config,
        registry=registry,
    )
    return run_service(aggregate)


if __name__ == "__main__":
    raise SystemExit(main())
