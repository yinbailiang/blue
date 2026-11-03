"""Agent 服务的运行时配置模型。

配置由 supervisor 写入 ``service_runtime_config.json``，服务启动时读取；
Agent 不直接读取核心 ``blue.infd``。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AgentConfig", "RegistryConfig"]


class AgentConfig(BaseModel):
    """Agent 的模型、会话与工具调用参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deepseek", "openai"] = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str | None = None
    system_prompt: str = "你是 blue，一个通过工具完成任务的智能体。"
    max_steps: int = Field(default=8, ge=1)
    tool_timeout: float = Field(default=30.0, gt=0)
    session_timeout: float = Field(default=10.0, gt=0)

    def resolve_api_key(self) -> str:
        """取访问密钥：优先配置内注入值，否则读取环境变量。

        :raises RuntimeConfigError: 两者都没有可用值。
        """
        if self.api_key:
            return self.api_key
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            from blue.errors import RuntimeConfigError

            raise RuntimeConfigError(
                f"未提供访问凭证：请在配置中填写 api_key，"
                f"或设置环境变量 {self.api_key_env!r}"
            )
        return api_key


class RegistryConfig(BaseModel):
    """工具注册中心租约参数（由 supervisor 通过环境变量注入）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: float = Field(default=15.0, gt=0)
    sweep_interval: float = Field(default=2.0, gt=0)
