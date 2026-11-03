"""browser 工具测试：图片归一化、参数校验与失败路径。"""

from __future__ import annotations

import asyncio
import io
import os
import random
import shutil

import pytest
from blue_service_browser import BrowserConfig, capture, configure
from blue_service_browser import _normalize as normalize  # pyright: ignore[reportPrivateUsage]
from PIL import Image


def _noisy_png(width: int, height: int) -> bytes:
    """生成一张「噪声图」，PNG 压缩率低，便于造出大文件。"""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    assert pixels is not None
    rng = random.Random(7)
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _flat_png(width: int, height: int) -> bytes:
    """生成一张纯色图（PNG 很小）。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (20, 30, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_normalize_keeps_small_png() -> None:
    """小图保持 PNG 且不缩放。"""
    data, mime, width, height = normalize(_flat_png(400, 300), max_edge=1568)
    assert mime == "image/png"
    assert (width, height) == (400, 300)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_normalize_downscales_to_max_edge() -> None:
    """超长边时等比缩小。"""
    _data, mime, width, height = normalize(_flat_png(2000, 1000), max_edge=800)
    assert max(width, height) == 800
    assert (width, height) == (800, 400)
    assert mime == "image/png"


def test_normalize_switches_to_jpeg_when_png_too_big() -> None:
    """PNG 超出体积预算时转 JPEG。"""
    data, mime, _, _ = normalize(_noisy_png(900, 900), max_edge=1568)
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    assert len(data) < 2_000_000


def test_capture_rejects_bad_url() -> None:
    """只接受 http/https 地址。"""
    configure(BrowserConfig())
    with pytest.raises(ValueError, match="http/https"):
        asyncio.run(capture("ftp://example.com/x"))
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(capture("   "))


@pytest.mark.skipif(
    shutil.which("firefox") is None
    and not os.path.exists("/run/current-system/sw/bin/firefox"),
    reason="环境里没有 firefox",
)
def test_capture_reports_unreachable_url() -> None:
    """连不上时给出明确错误，而不是抛底层异常。"""
    configure(BrowserConfig(timeout=8.0))
    with pytest.raises(ValueError, match=r"Firefox 截图失败|超时"):
        asyncio.run(capture("http://127.0.0.1:9/", 320, 240))
