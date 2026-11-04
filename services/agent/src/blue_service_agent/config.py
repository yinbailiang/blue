"""Agent 服务的运行时配置模型。

配置由 supervisor 写入 ``service_runtime_config.json``，服务启动时读取；
Agent 不直接读取核心 ``blue.infd``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from blue.errors import RuntimeConfigError
from blue_service_agent.compact import CompactRule

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
    outbox_max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "排队消息失败后最多尝试几次（含首次）；用完就留在发件箱里等人工处理"
        ),
    )
    tool_timeout: float = Field(default=30.0, gt=0)
    llm_timeout: float = Field(
        default=300.0,
        gt=0,
        description="模型请求的**读**超时（秒）。库默认 60s，长回答/大上下文容易"
        "读超时（aiohttp SocketTimeoutError）",
    )
    llm_connect_timeout: float = Field(
        default=10.0, gt=0, description="模型请求的连接超时（秒）"
    )
    llm_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="流式生成在**还没收到任何内容**时允许的重试次数（重放安全）",
    )
    llm_http_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "HTTP 层重试次数（429/5xx，由底层客户端退避重试）；"
            "与 llm_retries（流中断重放）是两套机制"
        ),
    )
    session_timeout: float = Field(default=10.0, gt=0)
    queue_timeout: float = Field(
        default=60.0,
        gt=0,
        description="同一会话并发请求的排队上限（秒），超时返回「会话繁忙」",
    )
    compact_history: bool = Field(
        default=True, description="发送前压缩历史里的工具往返（不改持久化数据）"
    )
    compact_keep_messages: int = Field(
        default=60, ge=0, description="尾部保留原文的消息条数（最近几轮不动）"
    )
    compact_text_chars: int = Field(
        default=200, ge=20, description="成功结果里大文本字段保留的字符数"
    )
    compact_error_chars: int = Field(
        default=600, ge=0, description="失败结果额外保留的结尾字符数"
    )
    compact_target_chars: int = Field(
        default=200_000,
        ge=0,
        description="提示目标体量（字符）；超预算时把最旧的消息降级成一行，0 = 不限",
    )
    compact_rules: list[CompactRule] = Field(
        default_factory=list[CompactRule],
        description="按工具定制的压缩规则（字段路径用 a.b 表示嵌套，tool 支持 * 通配）",
    )

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
