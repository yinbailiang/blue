"""blue WebUI 服务包。"""

from __future__ import annotations

from blue_service_webui.config import WebUIConfig
from blue_service_webui.service import WebUIService

__all__ = ["WebUIConfig", "WebUIService", "main"]

CONFIG_MODEL = WebUIConfig
"""契约测试使用的运行时配置模型。"""


def main() -> int:
    """控制台脚本入口（延迟导入）。"""
    from blue_service_webui.__main__ import main as _main

    return _main()
