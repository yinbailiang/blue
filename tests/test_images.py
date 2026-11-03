"""图片归一化测试：类型嗅探、等比缩放与体积预算。"""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from blue.images import ImagePayload, normalize_image, sniff_mime


def _png(width: int, height: int, *, noise: bool = False) -> bytes:
    """生成 PNG 字节；``noise`` 会让压缩率很差，便于造大文件。"""
    image = Image.new("RGB", (width, height), (20, 30, 40))
    if noise:
        pixels = image.load()
        assert pixels is not None
        rng = random.Random(11)
        for y in range(height):
            for x in range(width):
                pixels[x, y] = (
                    rng.randrange(256),
                    rng.randrange(256),
                    rng.randrange(256),
                )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    """生成 JPEG 字节。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 40, 10)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _gif() -> bytes:
    """生成 GIF 字节。"""
    buffer = io.BytesIO()
    Image.new("P", (10, 10)).save(buffer, format="GIF")
    return buffer.getvalue()


def test_sniff_mime_recognises_supported_formats() -> None:
    """按文件头嗅探四种受支持格式。"""
    assert sniff_mime(_png(4, 4)) == "image/png"
    assert sniff_mime(_jpeg(4, 4)) == "image/jpeg"
    assert sniff_mime(_gif()) == "image/gif"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_sniff_mime_rejects_others() -> None:
    """非图片内容返回 None。"""
    assert sniff_mime(b"%PDF-1.7") is None
    assert sniff_mime(b"hello world") is None
    assert sniff_mime(b"") is None


def test_normalize_keeps_small_png() -> None:
    """小图保持 PNG、不缩放。"""
    payload = normalize_image(_png(400, 300))
    assert isinstance(payload, ImagePayload)
    assert payload.mime == "image/png"
    assert (payload.width, payload.height) == (400, 300)
    assert payload.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_normalize_downscales_to_max_edge() -> None:
    """超长边等比缩小。"""
    payload = normalize_image(_png(2000, 1000), max_edge=800)
    assert (payload.width, payload.height) == (800, 400)
    assert payload.mime == "image/png"


def test_normalize_converts_big_png_to_jpeg() -> None:
    """PNG 超出体积预算时转 JPEG。"""
    payload = normalize_image(_png(900, 900, noise=True), max_edge=1568)
    assert payload.mime == "image/jpeg"
    assert payload.data[:2] == b"\xff\xd8"
    assert len(payload.data) < 2_000_000


def test_normalize_jpeg_stays_jpeg() -> None:
    """JPEG 输入不会被转成 PNG。"""
    payload = normalize_image(_jpeg(1200, 800), max_edge=600)
    assert payload.mime == "image/jpeg"
    assert max(payload.width, payload.height) == 600


def test_normalize_gif_takes_first_frame() -> None:
    """动图取首帧（输出 JPEG，因为 GIF 不重编码）。"""
    payload = normalize_image(_gif(), max_edge=1568)
    assert payload.mime == "image/jpeg"
    assert (payload.width, payload.height) == (10, 10)


def test_normalize_rejects_non_image() -> None:
    """非图片直接报错，而不是静默返回脏数据。"""
    with pytest.raises(ValueError, match="不是支持的图片格式"):
        normalize_image(b"not an image")
