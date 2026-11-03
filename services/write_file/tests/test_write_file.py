"""write_file 工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from blue_service_write_file import write_file


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    """父目录缺失时默认自动创建。"""
    target = tmp_path / "notes" / "a.txt"

    result = write_file(str(target), "hello")

    assert result.created is True
    assert result.bytes_written == 5
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_file_preserves_given_newlines(tmp_path: Path) -> None:
    """写入按原文落盘，不做换行风格转换。"""
    target = tmp_path / "a.txt"
    write_file(str(target), "a\r\nb\r\n")
    assert target.read_bytes() == b"a\r\nb\r\n"


def test_write_without_create_dirs_rejects_missing_parent(tmp_path: Path) -> None:
    """禁止自动创建目录时，父目录缺失应报错。"""
    with pytest.raises(ValueError, match="父目录不存在"):
        write_file(str(tmp_path / "missing" / "a.txt"), "x", create_dirs=False)


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    """覆盖已有文件时 created 为假。"""
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    result = write_file(str(target), "new")

    assert result.created is False
    assert target.read_text(encoding="utf-8") == "new"


def test_write_rejects_directory(tmp_path: Path) -> None:
    """目标为目录时拒绝写入。"""
    with pytest.raises(ValueError, match="目录"):
        write_file(str(tmp_path), "x")
