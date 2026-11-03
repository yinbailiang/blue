"""WebUI 服务进程入口。"""

from __future__ import annotations

from blue_contracts_session import EVENTS as SESSION_EVENTS
from blue_contracts_tool import EVENTS as TOOL_EVENTS

from blue.runtime import build_runtime, load_runtime_config, run_service
from blue_service_webui.config import WebUIConfig
from blue_service_webui.service import WebUIService

__all__ = ["main"]


def main() -> int:
    """进程入口。"""
    config = load_runtime_config(WebUIConfig)
    return run_service(
        WebUIService(
            config=config,
            runtime=build_runtime("webui", (*TOOL_EVENTS, *SESSION_EVENTS)),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
