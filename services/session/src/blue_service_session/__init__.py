"""blue session 服务包。"""

from __future__ import annotations

from blue_service_session.config import SessionConfig
from blue_service_session.db import AttachmentRecord, SessionDatabase
from blue_service_session.service import SessionAttachmentError, SessionService

__all__ = [
    "AttachmentRecord",
    "SessionAttachmentError",
    "SessionConfig",
    "SessionDatabase",
    "SessionService",
    "main",
]

CONFIG_MODEL = SessionConfig
"""契约测试使用的运行时配置模型。"""


def main() -> int:
    """控制台脚本入口（延迟导入）。"""
    from blue_service_session.__main__ import main as _main

    return _main()
