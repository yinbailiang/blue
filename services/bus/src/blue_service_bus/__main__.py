"""bus 服务入口。"""

from __future__ import annotations

from blue.runtime import load_runtime_config, run_service
from blue_service_bus.config import BusConfig
from blue_service_bus.service import BusService

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    config = load_runtime_config(BusConfig)
    return run_service(BusService(config))


if __name__ == "__main__":
    raise SystemExit(main())
