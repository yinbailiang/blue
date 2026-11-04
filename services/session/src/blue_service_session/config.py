"""session 服务运行时配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from blue.runtime import state_dir

__all__ = ["SessionConfig"]


class SessionConfig(BaseModel):
    """SQLite 会话存储参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_path: str = "sessions.db"
    max_sessions: int = Field(
        default=512, ge=1, description="会话数上限；仅在 prune_enabled 时生效"
    )
    idle_ttl: float = Field(
        default=7200.0, gt=0, description="空闲回收阈值；仅在 prune_enabled 时生效"
    )
    prune_enabled: bool = Field(
        default=False,
        description="自动回收总开关（默认关闭：不会自动删除会话或附件）",
    )
    prune_interval: float = Field(default=60.0, gt=0)
    prune_batch_size: int = Field(
        default=200, ge=1, description="单次回收的会话数上限（避免长事务）"
    )
    active_grace: float = Field(
        default=60.0, ge=0, description="容量淘汰时对最近活动会话的保护期（秒）"
    )
    busy_timeout_ms: int = Field(default=5000, ge=100)
    max_attachment_bytes: int = Field(default=2_097_152, ge=1024)
    max_attachments_per_message: int = Field(default=4, ge=1)
    attachment_grace: float = Field(default=3600.0, gt=0)

    def resolve_database_path(self) -> Path:
        """相对路径解析到 supervisor 状态目录（缺省 cwd/.blue）。"""
        path = Path(self.database_path).expanduser()
        if path.is_absolute():
            return path
        return (state_dir() / path).resolve()
