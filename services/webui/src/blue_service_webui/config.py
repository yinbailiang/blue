"""WebUI 运行时配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from blue.runtime import state_dir

__all__ = ["WebUIConfig"]


class WebUIConfig(BaseModel):
    """FastAPI WebUI 服务参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(default="127.0.0.1", description="监听地址")
    port: int = Field(default=8787, ge=1, le=65535, description="监听端口")
    title: str = Field(default="Blue Agent Studio", description="页面标题")
    request_timeout: float = Field(default=600.0, gt=0, description="Agent RPC 超时")
    poll_interval: float = Field(
        default=0.9, gt=0, description="等待 Agent 时轮询会话进度的间隔"
    )
    startup_timeout: float = Field(default=15.0, gt=0, description="HTTP 服务启动超时")

    def resolve_state_dir(self) -> Path:
        """解析 supervisor 状态目录，供系统状态面板读取。"""
        return state_dir()
