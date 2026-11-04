"""Agent 服务的运行时配置模型。

配置由 supervisor 写入 ``service_runtime_config.json``，服务启动时读取；
Agent 不直接读取核心 ``blue.infd``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from blue.errors import RuntimeConfigError

__all__ = ["AgentConfig"]


class AgentConfig(BaseModel):
    """Agent 的模型、会话与工具调用参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deepseek", "openai"] = "deepseek"
    model: str = "deepseek-chat"
    api_key: str | None = None
    base_url: str | None = None
    system_prompt: str = "你是 blue，一个通过工具完成任务的智能体。"
    max_steps: int = Field(default=8, ge=1)
    tool_timeout: float = Field(default=30.0, gt=0)
    llm_timeout: float = Field(
        default=300.0,
        gt=0,
        description="模型请求的**读**超时（秒）。库默认 60s，长回答/大上下文容易"
        "读超时（aiohttp SocketTimeoutError）",
    )
    llm_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="流式生成在**还没收到任何内容**时允许的重试次数（重放安全）",
    )
    session_timeout: float = Field(default=10.0, gt=0)

    def resolve_api_key(self) -> str:
        """取模型访问密钥。

        密钥来自 ``secrets.toml`` 的 ``[agent].api_key``，由 supervisor 写进
        运行时配置；服务不再回退到环境变量。

        :raises RuntimeConfigError: 未配置密钥。
        """
        if self.api_key:
            return self.api_key
        raise RuntimeConfigError(
            "未提供模型访问密钥：请在 secrets.toml 的 [agent].api_key 中配置"
            "（模板见 secrets.example.toml）"
        )
