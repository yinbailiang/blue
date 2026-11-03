"""配置层：用 infinity-data 编译 ``blue.infd``，再校验为强类型模型。

``.infd`` 负责语法与字段约束（类型、范围、枚举），Pydantic 负责跨字段与
运行时语义校验；两者失败都统一抛出 :class:`ConfigError`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from infinity_data import (
    CompilationResult,
    Diagnostic,
    SandboxConfig,
    Severity,
    load,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blue.errors import BlueError

DEFAULT_CONFIG_PATH = Path("blue.infd")
"""默认配置文件路径（相对当前工作目录）。"""

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AgentSettings",
    "BlueSettings",
    "BusSettings",
    "ConfigError",
    "RegistrySettings",
    "load_settings",
]


class ConfigError(BlueError):
    """配置缺失、编译失败或校验失败。"""


class AgentSettings(BaseModel):
    """Agent 核心的模型与会话参数。"""

    model_config = ConfigDict(frozen=True)

    provider: Literal["deepseek", "openai"] = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str | None = None
    system_prompt: str = "你是 blue，一个通过工具完成任务的智能体。"
    max_steps: int = Field(default=8, ge=1)
    tool_timeout: float = Field(default=30.0, gt=0)

    def resolve_api_key(self) -> str:
        """取访问密钥：优先配置内直接注入的 ``api_key``，否则读 ``api_key_env``。

        :raises ConfigError: 两者都没有可用值。
        """
        if self.api_key:
            return self.api_key
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise ConfigError(
                f"未提供访问凭证：请在配置中填写 api_key，"
                f"或设置环境变量 {self.api_key_env!r}"
            )
        return api_key


class BusSettings(BaseModel):
    """总线与传输参数。"""

    model_config = ConfigDict(frozen=True)

    transport: Literal["rabbit", "memory"] = "rabbit"
    url: str = "amqp://guest:guest@127.0.0.1:5672/"
    namespace: str = Field(default="blue", min_length=1)


class RegistrySettings(BaseModel):
    """工具注册中心的租约参数。"""

    model_config = ConfigDict(frozen=True)

    ttl: float = Field(default=15.0, gt=0)
    sweep_interval: float = Field(default=2.0, gt=0)


class BlueSettings(BaseModel):
    """``blue.infd`` 的完整配置。"""

    model_config = ConfigDict(frozen=True)

    agent: AgentSettings = Field(default_factory=AgentSettings)
    bus: BusSettings = Field(default_factory=BusSettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)


def load_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> BlueSettings:
    """编译并加载配置文件。

    :param path: ``.infd`` 配置路径。
    :raises ConfigError: 文件不存在、编译出错或字段校验失败。
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")

    result = _compile(config_path)
    if result.has_errors:
        raise ConfigError(_format_diagnostics(result.diagnostics))

    try:
        return BlueSettings.model_validate(result.value)
    except ValidationError as error:
        raise ConfigError(f"配置字段校验失败：{error}") from error


def _compile(config_path: Path) -> CompilationResult:
    """编译配置，并按需授权 ``!env import`` 读取的环境变量。

    沙盒默认零信任：先零授权编译一次，从 ``sandbox.env_unauthorized`` 诊断里
    收集配置真正用到（且已显式 ``!env import``）的变量名，再授权这些变量重新
    编译。配置文件本身是项目源码，读环境变量是显式声明的行为。
    """
    strict = load(config_path, sandbox=SandboxConfig(allow_env=[]))
    names = _requested_env_names(strict.diagnostics)
    if not names:
        return strict
    return load(config_path, sandbox=SandboxConfig(allow_env=names))


def _requested_env_names(diagnostics: list[Diagnostic]) -> list[str]:
    """从诊断中提取被沙盒拒绝的环境变量名。"""
    names = {
        str(item.params["name"])
        for item in diagnostics
        if item.code == "sandbox.env_unauthorized" and "name" in item.params
    }
    return sorted(names)


def _format_diagnostics(diagnostics: list[Diagnostic]) -> str:
    """把编译诊断（仅错误）渲染为多行文本。"""
    errors = [item for item in diagnostics if item.severity is Severity.ERROR]
    lines = [f"  - {item.location} [{item.code}] {item.message}" for item in errors]
    return "配置编译失败：\n" + "\n".join(lines)
