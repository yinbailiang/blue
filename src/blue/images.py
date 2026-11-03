"""图片归一化：把任意来源的图片压进模型与会话层的预算。

会话层对单个附件的上限是 2 MiB，DeepSeek 只接受 JPEG / PNG / GIF / WebP，
且每张图会被服务端缩到约 1300×1300、按图计费（上限 1024 token）。
所以任何要「给模型看」的图片都应先过一遍本模块：

- 长边超过 ``max_edge`` 等比缩小；
- PNG 超过 ``png_budget`` 时转 JPEG（同样的画面，体积小一个量级）；
- 动图只取首帧（模型只看静态图）。

Pillow 在函数内部按需导入：``blue`` 核心包不硬依赖它，只有真正处理图片的服务
（``browser`` / ``view_image``）在各自的 ``pyproject.toml`` 里声明 ``pillow``。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MAX_EDGE",
    "DEFAULT_PNG_BUDGET",
    "DEFAULT_QUALITY",
    "SUPPORTED_MIME",
    "ImagePayload",
    "normalize_image",
    "sniff_mime",
]

DEFAULT_MAX_EDGE = 1568
"""输出图片的长边上限（DeepSeek 会自动缩到 ~1300×1300，留一点余量）。"""

DEFAULT_PNG_BUDGET = 1_500_000
"""PNG 体积超过该值时转 JPEG，避免顶到 2 MiB 的附件上限。"""

DEFAULT_QUALITY = 85
"""转 JPEG 时的质量。"""

SUPPORTED_MIME: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
)
"""DeepSeek 支持的图片格式（按内容嗅探，不看扩展名）。"""


@dataclass(frozen=True)
class ImagePayload:
    """归一化后的图片。"""

    data: bytes
    """编码后的字节。"""

    mime: str
    """实际的 MIME（``image/png`` 或 ``image/jpeg``）。"""

    width: int
    """输出宽度。"""

    height: int
    """输出高度。"""


def sniff_mime(data: bytes) -> str | None:
    """按文件头嗅探图片类型；不是支持的图片时返回 ``None``。

    :param data: 原始字节
    :return: ``image/jpeg`` / ``image/png`` / ``image/gif`` / ``image/webp`` 之一
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalize_image(
    data: bytes,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    png_budget: int = DEFAULT_PNG_BUDGET,
    quality: int = DEFAULT_QUALITY,
) -> ImagePayload:
    """把图片缩到长边 ``max_edge`` 内，并在必要时转成 JPEG。

    :param data: 原始图片字节
    :param max_edge: 长边上限
    :param png_budget: PNG 体积阈值，超过就转 JPEG
    :param quality: JPEG 质量
    :return: 归一化后的图片
    :raises ValueError: 不是支持的图片格式，或 Pillow 无法解码
    """
    mime = sniff_mime(data)
    if mime is None:
        raise ValueError("不是支持的图片格式（仅 JPEG / PNG / GIF / WebP）")

    from PIL import Image  # 延迟导入：核心包不硬依赖 Pillow

    try:
        with Image.open(io.BytesIO(data)) as raw:
            image = raw.convert("RGB")
            if max(image.width, image.height) > max_edge:
                # thumbnail 原地等比缩放，避免依赖 Pillow 的 resize 类型标注
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            if mime == "image/png" and len(data) <= png_budget:
                image.save(buffer, format="PNG", optimize=True)
                out_mime = "image/png"
            else:
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                out_mime = "image/jpeg"
            return ImagePayload(
                data=buffer.getvalue(),
                mime=out_mime,
                width=image.width,
                height=image.height,
            )
    except ValueError:
        raise
    except Exception as error:  # Pillow 的异常类型很杂，统一降级成参数错误
        raise ValueError(f"图片解码失败：{error}") from error
