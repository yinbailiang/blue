"""view_image 工具测试：路径解析、体积/格式校验与附件产出。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from blue_service_view_image import ViewImageConfig, configure, view_image
from PIL import Image

from blue.runtime import PROJECT_ROOT_ENV


def _write_png(path: Path, width: int = 800, height: int = 600) -> None:
    """在 ``path`` 写一张纯色 PNG。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())


def test_views_image_and_returns_attachment(tmp_path: Path) -> None:
    """读图并返回图片附件（mime/data 可解码）。"""
    configure(ViewImageConfig())
    target = tmp_path / "shot.png"
    _write_png(target)

    result = view_image(str(target))
    assert result.path == str(target.resolve())
    assert (result.width, result.height) == (800, 600)
    assert result.mime == "image/png"
    assert len(result.attachments) == 1
    blob = result.attachments[0]
    assert blob["mime"] == "image/png"
    assert base64.b64decode(blob["data"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_relative_path_anchors_at_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径锚定项目根，而不是进程 cwd。"""
    monkeypatch.setenv(PROJECT_ROOT_ENV, str(tmp_path))
    configure(ViewImageConfig())
    _write_png(tmp_path / "chart.png", 400, 200)

    result = view_image("chart.png")
    assert result.path == str((tmp_path / "chart.png").resolve())
    assert (result.width, result.height) == (400, 200)


def test_downscales_to_configured_edge(tmp_path: Path) -> None:
    """超过 max_edge 的图片会被等比缩小。"""
    configure(ViewImageConfig(max_edge=500))
    target = tmp_path / "big.png"
    _write_png(target, 2000, 1000)

    result = view_image(str(target))
    assert (result.width, result.height) == (500, 250)


def test_rejects_missing_file(tmp_path: Path) -> None:
    """文件不存在时报错。"""
    configure(ViewImageConfig())
    with pytest.raises(ValueError, match="不存在"):
        view_image(str(tmp_path / "nope.png"))


def test_rejects_non_image(tmp_path: Path) -> None:
    """不是图片时报错。"""
    configure(ViewImageConfig())
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="不是支持的图片格式"):
        view_image(str(target))


def test_rejects_oversize_file(tmp_path: Path) -> None:
    """超过体积上限时提前拒绝（不读进内存再失败）。"""
    configure(ViewImageConfig(max_bytes=1024))
    target = tmp_path / "huge.png"
    _write_png(target, 500, 500)
    with pytest.raises(ValueError, match="过大"):
        view_image(str(target))


def test_rejects_empty_path() -> None:
    """空路径直接拒绝。"""
    configure(ViewImageConfig())
    with pytest.raises(ValueError, match="不能为空"):
        view_image("   ")
