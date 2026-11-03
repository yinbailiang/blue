"""session 服务进程入口。"""

from __future__ import annotations

from blue_contracts_session import EVENTS as SESSION_EVENTS

from blue.runtime import build_runtime, load_runtime_config, run_service
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    config = load_runtime_config(SessionConfig)
    return run_service(
        SessionService(config=config, runtime=build_runtime("session", SESSION_EVENTS))
    )


if __name__ == "__main__":
    raise SystemExit(main())
