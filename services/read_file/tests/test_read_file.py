"""read_file 工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from blue_service_read_file import read_file


def test_read_file_line_range_and_truncation(tmp_path: Path) -> None:
    """按行范围读取并标记是否截断。"""
    target = tmp_path / "a.txt"
    target.write_text("1\n2\n3\n4\n5\n", encoding="utf-8")

    result = read_file(str(target), start_line=2, max_lines=2)
    assert result.content == "2\n3"
    assert result.start_line == 2
    assert result.end_line == 3
    assert result.total_lines == 5
    assert result.truncated is True

    tail = read_file(str(target), start_line=4)
    assert tail.content == "4\n5"
    assert tail.truncated is False


def test_read_file_empty(tmp_path: Path) -> None:
    """空文件返回空内容且不报错。"""
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = read_file(str(target))
    assert result.content == ""
    assert result.total_lines == 0
    assert result.end_line == 0
    assert result.truncated is False


def test_read_file_missing_file(tmp_path: Path) -> None:
    """文件不存在时报错。"""
    with pytest.raises(ValueError, match="不存在"):
        read_file(str(tmp_path / "nope.txt"))


def test_read_file_rejects_start_line_out_of_range(tmp_path: Path) -> None:
    """起始行号超出总行数时报错。"""
    target = tmp_path / "a.txt"
    target.write_text("only\n", encoding="utf-8")

    with pytest.raises(ValueError, match="超出"):
        read_file(str(target), start_line=2)


def test_read_file_supports_absolute_path(tmp_path: Path) -> None:
    """绝对路径被保留并解析。"""
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    result = read_file(str(target))
    assert result.path == str(target.resolve())
