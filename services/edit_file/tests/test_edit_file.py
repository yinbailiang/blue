"""edit_file 工具测试。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from blue_service_edit_file import edit_file

import blue.runtime as runtime
from blue.runtime import WorkspaceConfig, configure_workspace


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


@pytest.fixture(autouse=True)
def _isolate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """``configure_workspace()`` 写入的进程级状态不应泄漏到其它用例。"""
    monkeypatch.setattr(runtime, "_workspace", None)


def test_relative_path_anchors_project_root_not_service_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    install_infra: Callable[..., None],
) -> None:
    """回归：相对路径改的是项目根下的文件，而不是服务目录里的同名文件。"""
    project = tmp_path / "project"
    service_dir = project / "services" / "edit_file"
    service_dir.mkdir(parents=True)
    target = project / "notes.txt"
    target.write_text("alpha\n", encoding="utf-8")
    decoy = service_dir / "notes.txt"
    decoy.write_text("alpha\n", encoding="utf-8")
    install_infra(project)
    monkeypatch.chdir(service_dir)

    result = edit_file("notes.txt", "alpha", "beta")

    assert result.path == str(target.resolve())
    assert target.read_text(encoding="utf-8") == "beta\n"
    assert decoy.read_text(encoding="utf-8") == "alpha\n"


def test_relative_path_honours_configured_workdir(
    tmp_path: Path, install_infra: Callable[..., None]
) -> None:
    """workdir 配置为相对路径时按项目根解析。"""
    root = tmp_path / "root"
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("alpha\n", encoding="utf-8")
    install_infra(root)
    configure_workspace(WorkspaceConfig(workdir="sub"))

    result = edit_file("a.txt", "alpha", "beta")

    assert result.path == str((nested / "a.txt").resolve())
