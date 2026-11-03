"""blue 的错误类型。"""

from __future__ import annotations

__all__ = ["BlueError"]


class BlueError(Exception):
    """blue 面向用户的错误基类：CLI 会把消息打印为 ``错误：...``。"""
