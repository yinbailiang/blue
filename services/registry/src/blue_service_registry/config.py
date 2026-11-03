"""工具运行时注册中心的运行时配置。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["RegistryConfig"]


class RegistryConfig(BaseModel):
    """租约与巡检参数（由 supervisor 通过环境变量注入）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: float = Field(default=15.0, gt=0)
    sweep_interval: float = Field(default=2.0, gt=0)
