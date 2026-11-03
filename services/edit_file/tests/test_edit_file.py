"""edit_file 工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from blue_service_edit_file import edit_file


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
