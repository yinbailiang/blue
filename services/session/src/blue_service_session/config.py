"""session 服务运行时配置。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SessionConfig"]


class SessionConfig(BaseModel):
    """SQLite 会话存储参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_path: str = "sessions.db"
    max_sessions: int = Field(default=512, ge=1)
    idle_ttl: float = Field(default=7200.0, gt=0)
    prune_interval: float = Field(default=60.0, gt=0)
    busy_timeout_ms: int = Field(default=5000, ge=100)

    def resolve_database_path(self) -> Path:
        """相对路径解析到 ``BLUE_STATE_DIR``（缺省 cwd/.blue）。"""
        path = Path(self.database_path).expanduser()
        if path.is_absolute():
            return path
        state_dir = os.environ.get("BLUE_STATE_DIR")
        base = Path(state_dir) if state_dir else (Path.cwd() / ".blue")
        return (base / path).resolve()
