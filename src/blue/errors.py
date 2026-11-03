"""blue 的错误类型。"""

from __future__ import annotations

__all__ = [
    "BlueError",
    "ConfigError",
    "RuntimeConfigError",
    "SupervisorError",
]


class BlueError(Exception):
    """blue 面向用户的错误基类：CLI 会把消息打印为 ``错误：...``。"""


class ConfigError(BlueError):
    """核心配置编译、结构或依赖校验失败。"""


class RuntimeConfigError(BlueError):
    """服务运行时配置缺失、不可读或校验失败。"""


class SupervisorError(BlueError):
    """supervisor 无法启动、无法监管或无法完成调谐。"""
