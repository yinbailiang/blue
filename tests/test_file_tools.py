"""文件工具测试：读取、写入与精确替换。"""

from __future__ import annotations

from pathlib import Path

import pytest
from handlers.tools.edit_file import edit_file
from handlers.tools.read_file import read_file
from handlers.tools.write_file import write_file


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """写入后能按行读回，并报告行数与截断状态。"""
    target = tmp_path / "notes" / "a.txt"
    written = write_file(str(target), "第一行\n第二行\n")
    assert written.created is True
    assert written.bytes_written == len("第一行\n第二行\n".encode())
    assert target.read_text(encoding="utf-8") == "第一行\n第二行\n"

    read = read_file(str(target))
    assert read.path == str(target.resolve())
    assert read.content == "第一行\n第二行"
    assert read.total_lines == 2
    assert read.start_line == 1
    assert read.end_line == 2
    assert read.truncated is False


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


def test_edit_file_replaces_unique_match(tmp_path: Path) -> None:
    """唯一匹配时完成精确替换。"""
    target = tmp_path / "a.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_file(str(target), "beta", "gamma")
    assert result.replacements == 1
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_edit_file_requires_unique_match(tmp_path: Path) -> None:
    """多处匹配且未开启 replace_all 时报错。"""
    target = tmp_path / "a.txt"
    target.write_text("x\nx\n", encoding="utf-8")

    with pytest.raises(ValueError, match="2 处"):
        edit_file(str(target), "x", "y")


def test_edit_file_replace_all(tmp_path: Path) -> None:
    """replace_all 替换全部匹配。"""
    target = tmp_path / "a.txt"
    target.write_text("x\nx\n", encoding="utf-8")

    result = edit_file(str(target), "x", "y", replace_all=True)
    assert result.replacements == 2
    assert target.read_text(encoding="utf-8") == "y\ny\n"


def test_edit_file_missing_text(tmp_path: Path) -> None:
    """原文不存在时报错。"""
    target = tmp_path / "a.txt"
    target.write_text("alpha\n", encoding="utf-8")

    with pytest.raises(ValueError, match="未找到"):
        edit_file(str(target), "beta", "gamma")


def test_edit_file_empty_old_text_rejected(tmp_path: Path) -> None:
    """空原文无法定位，直接报错。"""
    target = tmp_path / "a.txt"
    target.write_text("alpha\n", encoding="utf-8")

    with pytest.raises(ValueError, match="不能为空"):
        edit_file(str(target), "", "gamma")


def test_edit_file_preserves_crlf(tmp_path: Path) -> None:
    """CRLF 文件可用 LF 形式的原文匹配，替换后仍保持 CRLF。"""
    target = tmp_path / "a.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    result = edit_file(str(target), "alpha\nbeta", "gamma\ndelta")
    assert result.replacements == 1
    assert target.read_bytes() == b"gamma\r\ndelta\r\n"
