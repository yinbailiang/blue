"""重启退避策略。"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["Backoff", "should_restart"]


@dataclass(frozen=True)
class Backoff:
    """指数退避 + 抖动。

    第 ``attempt`` 次失败（从 1 开始）的延迟为
    ``min(maximum, base * 2 ** (attempt - 1))``，再叠加最多 20% 的抖动，
    最终不超过 ``maximum``。
    """

    base: float = 0.5
    maximum: float = 30.0
    jitter: Callable[[], float] = field(default=random.random)

    def __post_init__(self) -> None:
        if self.base <= 0:
            raise ValueError("base 必须 > 0")
        if self.maximum < self.base:
            raise ValueError("maximum 不能小于 base")

    def delay(self, attempt: int) -> float:
        """计算第 ``attempt`` 次连续失败后的延迟（秒）。"""
        exponent = max(0, attempt - 1)
        raw = min(self.maximum, self.base * (2**exponent))
        jitter = raw * 0.2 * min(max(self.jitter(), 0.0), 1.0)
        return min(self.maximum, raw + jitter)


def should_restart(policy: str, exit_code: int | None) -> bool:
    """按重启策略判断退出后是否应重启。

    :param policy: ``always`` / ``on-failure`` / ``never``。
    :param exit_code: 退出码；``None`` 表示被信号终止或未知。
    """
    if policy == "always":
        return True
    if policy == "never":
        return False
    return exit_code != 0
