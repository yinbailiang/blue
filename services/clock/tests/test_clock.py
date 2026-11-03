"""clock 工具测试。"""

from __future__ import annotations

from blue_service_clock import now


def test_clock_utc() -> None:
    """UTC 结果包含时区且时间戳为正。"""
    result = now("utc")
    assert result.zone == "utc"
    assert result.iso.endswith("+00:00")
    assert result.epoch > 0


def test_clock_local() -> None:
    """本地时间结果可用。"""
    result = now("local")
    assert result.zone == "local"
    assert result.epoch > 0
