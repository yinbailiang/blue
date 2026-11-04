"""运行日志配置测试：服务进程默认能看见 INFO 日志。"""

from __future__ import annotations

import logging

import pytest

from blue.runtime import configure_logging


def test_configure_logging_sets_level_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BLUE_LOG_LEVEL`` 生效；重复调用幂等不抛错。"""
    root = logging.getLogger()
    saving = (root.level, list(root.handlers))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        monkeypatch.setenv("BLUE_LOG_LEVEL", "debug")
        configure_logging()
        assert root.level == logging.DEBUG
        assert logging.getLogger("blue.test").isEnabledFor(logging.INFO)
        # 重复调用不抛错（basicConfig 幂等）
        configure_logging()
        assert root.level == logging.DEBUG
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saving[1]:
            root.addHandler(handler)
        root.setLevel(saving[0])


def test_configure_logging_falls_back_on_unknown_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知级别退回 INFO，而不是把服务日志关掉。"""
    root = logging.getLogger()
    saving = (root.level, list(root.handlers))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        monkeypatch.setenv("BLUE_LOG_LEVEL", "loud")
        configure_logging()
        assert root.level == logging.INFO
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saving[1]:
            root.addHandler(handler)
        root.setLevel(saving[0])
