"""``LogManager``：日志写入、按大小轮转与 tail / follow。

为什么这些用例值钱：``.blue/logs/<service>.log`` 是排障的第一现场（服务起不来、
崩溃循环、冒烟失败都只能看它），轮转写坏了会**悄悄**丢掉最需要的那段历史。
此前这个模块只有 53% 覆盖，``_rotate`` 的三条分支（正常轮转 / ``backups = 0`` /
文件不存在）从没被执行过——门是绿的，但那段代码从没跑过。
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from blue.control.logs import LogManager


def test_append_prefixes_timestamp_and_stream(tmp_path: Path) -> None:
    """每行都带 ISO 时间戳与流名，且缺失的换行会被补上。"""
    logs = LogManager(tmp_path)
    asyncio.run(logs.append("svc", "stdout", "hello\nworld"))
    text = logs.path_for("svc").read_text(encoding="utf-8")
    lines = text.splitlines()

    assert len(lines) == 2
    assert lines[0].endswith("[stdout] hello")
    assert lines[1].endswith("[stdout] world")
    assert lines[0].startswith("20")
    assert text.endswith("\n")


def test_append_empty_text_is_noop(tmp_path: Path) -> None:
    """空串不建文件：服务没输出时不该凭空造出空日志。"""
    logs = LogManager(tmp_path)
    asyncio.run(logs.append("svc", "stdout", ""))
    assert not logs.path_for("svc").exists()


def test_invalid_settings_are_rejected(tmp_path: Path) -> None:
    """``max_bytes`` 必须为正、``backups`` 不能为负——写错要当场报，别静默不轮转。"""
    with pytest.raises(ValueError, match="max_bytes"):
        LogManager(tmp_path, max_bytes=0)
    with pytest.raises(ValueError, match="backups"):
        LogManager(tmp_path, backups=-1)


def test_rotate_shifts_backups_in_order(tmp_path: Path) -> None:
    """到顶后轮转：``.1`` / ``.2`` 依次出现，超出的最老档不保留。"""
    logs = LogManager(tmp_path, max_bytes=200, backups=2)
    for index in range(20):
        asyncio.run(logs.append("svc", "stdout", f"line-{index}"))

    path = logs.path_for("svc")
    assert path.exists()
    assert path.stat().st_size <= 200
    assert path.with_name("svc.log.1").exists()
    assert path.with_name("svc.log.2").exists()
    # 超过 backups 的更老档不保留
    assert not path.with_name("svc.log.3").exists()
    # 轮转后当前日志里只剩最新那一行
    assert path.read_text(encoding="utf-8").rstrip().endswith("line-19")


def test_rotate_without_backups_truncates(tmp_path: Path) -> None:
    """``backups = 0`` 表示不保留历史：到顶直接丢掉整份，而不是无限增长。"""
    logs = LogManager(tmp_path, max_bytes=100, backups=0)
    asyncio.run(logs.append("svc", "stdout", "x" * 80))
    asyncio.run(logs.append("svc", "stdout", "y" * 80))

    path = logs.path_for("svc")
    assert path.exists()
    assert not path.with_name("svc.log.1").exists()
    text = path.read_text(encoding="utf-8")
    assert "y" * 80 in text
    assert "x" * 80 not in text


def test_tail_returns_last_lines_and_handles_missing(tmp_path: Path) -> None:
    """``tail`` 取末尾若干行；文件不存在或为空都返回空列表。"""
    logs = LogManager(tmp_path)
    assert logs.tail("missing") == []

    logs.path_for("svc").write_text("", encoding="utf-8")
    assert logs.tail("svc") == []

    for index in range(10):
        asyncio.run(logs.append("svc", "stdout", f"line-{index}"))
    last_three = logs.tail("svc", lines=3)
    assert len(last_three) == 3
    assert last_three[-1].endswith("line-9")


def test_follow_yields_new_lines(tmp_path: Path) -> None:
    """``follow`` 从文件末尾开始，只产出之后写进来的行。"""

    async def scenario() -> list[str]:
        logs = LogManager(tmp_path)
        await logs.append("svc", "stdout", "old")
        received: list[str] = []

        async def consume() -> None:
            async with aclosing(logs.follow("svc")) as stream:
                async for line in stream:
                    received.append(line)
                    return

        task = asyncio.create_task(consume())
        # 让 follow 先跑到文件末尾，再写新内容；否则新行在 seek(END) 之前就落地了。
        await asyncio.sleep(0.3)
        await logs.append("svc", "stdout", "new-1")
        async with asyncio.timeout(5):
            await task
        return received

    assert asyncio.run(scenario())[0].endswith("[stdout] new-1")
