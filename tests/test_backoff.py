"""退避与重启策略测试。"""

from __future__ import annotations

import pytest

from blue.control.backoff import Backoff, should_restart


def test_backoff_grows_and_caps() -> None:
    """指数增长但不超过上限。"""
    backoff = Backoff(base=0.5, maximum=4.0, jitter=lambda: 0.0)
    assert backoff.delay(1) == pytest.approx(0.5)
    assert backoff.delay(2) == pytest.approx(1.0)
    assert backoff.delay(3) == pytest.approx(2.0)
    assert backoff.delay(4) == pytest.approx(4.0)
    assert backoff.delay(10) == pytest.approx(4.0)


def test_backoff_jitter_is_bounded() -> None:
    """抖动最多 20%，且不超过上限。"""
    backoff = Backoff(base=1.0, maximum=10.0, jitter=lambda: 1.0)
    assert backoff.delay(1) == pytest.approx(1.2)
    assert backoff.delay(100) == pytest.approx(10.0)


def test_backoff_rejects_invalid_configuration() -> None:
    """非法参数立即报错。"""
    with pytest.raises(ValueError):
        Backoff(base=0)
    with pytest.raises(ValueError):
        Backoff(base=2, maximum=1)


@pytest.mark.parametrize(
    ("policy", "exit_code", "expected"),
    [
        ("always", 0, True),
        ("always", 1, True),
        ("on-failure", 0, False),
        ("on-failure", 1, True),
        ("on-failure", None, True),
        ("never", 0, False),
        ("never", 1, False),
    ],
)
def test_should_restart(policy: str, exit_code: int | None, expected: bool) -> None:
    """重启策略矩阵。"""
    assert should_restart(policy, exit_code) is expected
