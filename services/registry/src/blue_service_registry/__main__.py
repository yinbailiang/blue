"""工具运行时注册中心进程入口。"""

from __future__ import annotations

from blue_contracts_tool import EVENTS as TOOL_EVENTS

from blue.runtime import build_runtime, load_infra_config, run_service
from blue_service_registry.config import RegistryConfig
from blue_service_registry.registry import ToolRegistryService

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    infra = load_infra_config()
    config = RegistryConfig(
        ttl=infra.registry_ttl,
        sweep_interval=infra.registry_sweep_interval,
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
