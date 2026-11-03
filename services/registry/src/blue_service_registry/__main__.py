"""工具运行时注册中心进程入口。"""

from __future__ import annotations

import os

from blue_contracts_tool import EVENTS as TOOL_EVENTS

from blue.runtime import build_runtime, run_service
from blue_service_registry.config import RegistryConfig
from blue_service_registry.registry import ToolRegistryService

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    config = RegistryConfig(
        ttl=float(os.environ.get("BLUE_REGISTRY_TTL", "15")),
        sweep_interval=float(os.environ.get("BLUE_REGISTRY_SWEEP_INTERVAL", "2")),
    )
    return run_service(
        ToolRegistryService(
            runtime=build_runtime("registry", TOOL_EVENTS),
            ttl=config.ttl,
            sweep_interval=config.sweep_interval,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
