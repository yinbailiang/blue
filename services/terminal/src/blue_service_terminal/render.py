"""把 VT 屏幕渲染成 PNG，并提供像素↔行列的几何换算。

模型看不懂 ANSI 转义序列，但看得懂图。所以终端会话除了给文本，还能给一张
"终端长这样"的截图；点击/拖动也复用同一套几何，把截图上的像素换算回行列。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import wcwidth
from PIL import Image, ImageDraw, ImageFont

from blue_service_terminal.colors import (
    DEFAULT_BACKGROUND,
    DEFAULT_FOREGROUND,
    ansi_palette,
)
from blue_service_terminal.session import CellStyle

__all__ = [
    "FontSet",
    "FrameGeometry",
    "render_screen",
    "resolve_fonts",
]

DEFAULT_FG = DEFAULT_FOREGROUND
DEFAULT_BG = DEFAULT_BACKGROUND
CURSOR_BG = "#93c5fd"
CURSOR_FG = DEFAULT_BACKGROUND

_BRIGHT_SUFFIX = "bright"


@dataclass(frozen=True)
class FrameGeometry:
    """渲染几何：像素与行列互转的唯一依据。"""

    cell_width: int
    cell_height: int
    padding: int
    cols: int
    rows: int

    @property
    def width(self) -> int:
        """图像宽度（像素）。"""
        return self.padding * 2 + self.cols * self.cell_width

    @property
    def height(self) -> int:
        """图像高度（像素）。"""
        return self.padding * 2 + self.rows * self.cell_height

    def to_cell(self, x: int, y: int) -> tuple[int, int]:
        """像素坐标 → ``(col, row)``，1 起并夹在屏幕内。"""
        col = (int(x) - self.padding) // max(1, self.cell_width) + 1
        row = (int(y) - self.padding) // max(1, self.cell_height) + 1
        return max(1, min(col, self.cols)), max(1, min(row, self.rows))

    def to_pixel(self, col: int, row: int) -> tuple[int, int]:
        """``(col, row)``（1 起）→ 该格中心的像素坐标。"""
        x = self.padding + (col - 1) * self.cell_width + self.cell_width // 2
        y = self.padding + (row - 1) * self.cell_height + self.cell_height // 2
        return x, y


_CANDIDATE_FONTS = (
    "/run/current-system/sw/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
)
"""常见等宽字体路径；NixOS 走第一个（若已安装）。"""

_CANDIDATE_CJK_FONTS = (
    "/run/current-system/sw/share/fonts/opentype/noto-cjk/NotoSansCJK-VF.otf.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
"""CJK 回退字体：拉丁等宽字体没有汉字字形，中文会画成方块。"""

_CJK_GLOBS = (
    "*noto-fonts-cjk-sans*/share/fonts/opentype/noto-cjk/NotoSansCJK-VF.otf.ttc",
    "*noto-fonts-cjk*/share/fonts/opentype/noto-cjk/NotoSansCJK-Regular.ttc",
)

_CANDIDATE_SYMBOL_FONTS = (
    "/usr/share/fonts/truetype/NerdFonts/Symbols/SymbolsNerdFontMono-Regular.ttf",
)
"""符号回退字体：覆盖私用区（PUA）图标。"""

_SYMBOL_GLOBS = (
    # Nerd Font 的符号字体优先，退而求其次用带图标的等宽 Nerd 字体
    "*nerd-fonts*/share/fonts/truetype/NerdFonts/Symbols/SymbolsNerdFontMono-Regular.ttf",
    "*nerd-fonts-fira-code*/share/fonts/truetype/NerdFonts/FiraCode/FiraCodeNerdFontMono-Regular.ttf",
)
"""nvim / lualine 状态栏、Powerline 分隔符都画在私用区（U+E000–U+F8FF），
普通等宽字体没有这些字形，会渲染成空心方块。"""


def resolve_fonts(
    configured: str | None = None,
    configured_cjk: str | None = None,
    configured_symbol: str | None = None,
) -> tuple[str, str, str]:
    """返回 ``(等宽主字体, CJK 回退字体, 符号回退字体)``；找不到时给空串。

    三个都不配（``None``，即 infd 里的 ``<path?>``）表示自动探测。
    """
    return (
        _pick(
            configured or "",
            _CANDIDATE_FONTS,
            ("*dejavu*/share/fonts/truetype/DejaVuSansMono.ttf",),
        ),
        _pick(configured_cjk or "", _CANDIDATE_CJK_FONTS, _CJK_GLOBS),
        _pick(configured_symbol or "", _CANDIDATE_SYMBOL_FONTS, _SYMBOL_GLOBS),
    )


def _pick(configured: str, candidates: tuple[str, ...], globs: tuple[str, ...]) -> str:
    """按「显式配置 → 常见路径 → nix store 通配」的顺序找一个存在的字体文件。"""
    text = configured.strip()
    if text:
        return text if Path(text).is_file() else ""
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    for root in ("/nix/store", "/usr/share/fonts"):
        base = Path(root)
        if not base.is_dir():
            continue
        for pattern in globs:
            for path in base.glob(pattern):
                return str(path)
    return ""


@dataclass(frozen=True)
class FontSet:
    """渲染用的字体组合：拉丁走等宽主字体，宽字符走 CJK，私用区图标走符号字体。"""

    main_path: str
    cjk_path: str
    size: int
    symbol_path: str = ""

    def main(self) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """主字体。"""
        return _font(self.main_path, self.size)

    def cjk(self) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
        """CJK 字体（没有就返回 ``None``，退回主字体画方块）。"""
        if not self.cjk_path:
            return None
        return _cjk_font(self.cjk_path, self.size)

    def symbol(self) -> ImageFont.FreeTypeFont | ImageFont.ImageFont | None:
        """符号字体：画私用区图标（没有就返回 ``None``，退回主字体画空心方块）。"""
        if not self.symbol_path:
            return None
        return _symbol_font(self.symbol_path, self.size)


@lru_cache(maxsize=8)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载字体（带缓存）。"""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=8)
def _symbol_font(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    """加载符号字体（Nerd Font）：私用区图标靠它才画得出来。"""
    if not path:
        return None
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


@lru_cache(maxsize=8)
def _cjk_font(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    """加载 CJK 字体；``.ttc`` 里挑中文那一款（family 含 ``SC``）。"""
    if not path:
        return None
    if not path.endswith((".ttc", ".otc")):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            return None
    fallback: ImageFont.FreeTypeFont | None = None
    for index in range(8):
        try:
            font = ImageFont.truetype(path, size, index=index)
        except OSError:
            break
        family = " ".join(part for part in font.getname() if part)
        if fallback is None:
            fallback = font
        if " SC" in family or family.endswith("SC"):
            return font
    return fallback


def geometry_for(
    *, cols: int, rows: int, font_path: str, font_size: int
) -> FrameGeometry:
    """按字体度量算出图片几何。"""
    font = _font(font_path, font_size)
    sample = font.getlength("M") if hasattr(font, "getlength") else font_size * 0.6
    cell_width = max(6, round(sample))
    cell_height = max(10, round(font_size * 1.35))
    return FrameGeometry(
        cell_width=cell_width,
        cell_height=cell_height,
        padding=8,
        cols=cols,
        rows=rows,
    )


def _is_private_use(char: str) -> bool:
    """私用区码点：Nerd Font 图标与 Powerline 分隔符都落在这里。"""
    if len(char) != 1:
        return False
    code = ord(char)
    return (
        0xE000 <= code <= 0xF8FF
        or 0xF0000 <= code <= 0xFFFFD
        or 0x100000 <= code <= 0x10FFFD
    )


def _choose_font(
    char: str,
    *,
    main: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    cjk: ImageFont.FreeTypeFont | ImageFont.ImageFont | None,
    symbol: ImageFont.FreeTypeFont | ImageFont.ImageFont | None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按字符挑字体：宽字符 → CJK，私用区 → 符号字体，其余 → 主字体。"""
    if len(char) == 1:
        if wcwidth.wcwidth(char) == 2 and cjk is not None:
            return cjk
        if symbol is not None and _is_private_use(char):
            return symbol
    return main


def render_screen(
    cells: list[list[CellStyle]],
    geometry: FrameGeometry,
    *,
    fonts: FontSet,
    background: str = DEFAULT_BACKGROUND,
    foreground: str = DEFAULT_FOREGROUND,
    palette: dict[str, str] | None = None,
) -> bytes:
    """把字符格画成 PNG，返回字节（宽字符用 CJK 字体，避免画成方块）。

    :param cells: 屏幕字符格
    :param geometry: 像素几何
    :param fonts: 字体组合
    :param background: 背景色（``#rrggbb``），同时决定未着色格的底色
    :param foreground: 默认前景色（``#rrggbb``）
    :param palette: 覆盖 ANSI 基本色的映射，缺省用内置调色板
    """
    font = fonts.main()
    cjk = fonts.cjk()
    symbol = fonts.symbol()
    colors = ansi_palette(palette)
    image = Image.new("RGB", (geometry.width, geometry.height), background)
    draw = ImageDraw.Draw(image)

    for row_index, row in enumerate(cells):
        top = geometry.padding + row_index * geometry.cell_height
        col_index = 0
        while col_index < len(row):
            cell = row[col_index]
            cell_bg = _resolve(cell.bg, default=background, palette=colors)
            # 合并连续的同底色格，减少绘制次数
            span = 1
            while col_index + span < len(row) and _same_bg(row[col_index + span], cell):
                span += 1
            if cell_bg != background:
                draw.rectangle(
                    [
                        geometry.padding + col_index * geometry.cell_width,
                        top,
                        geometry.padding + (col_index + span) * geometry.cell_width - 1,
                        top + geometry.cell_height - 1,
                    ],
                    fill=cell_bg,
                )
            col_index += span

    for row_index, row in enumerate(cells):
        top = geometry.padding + row_index * geometry.cell_height
        for col_index, cell in enumerate(row):
            if not cell.char:
                continue
            left = geometry.padding + col_index * geometry.cell_width
            foreground_color = _resolve(
                cell.fg, default=foreground, palette=colors, bold=cell.bold
            )
            background_color = _resolve(cell.bg, default=background, palette=colors)
            if cell.reverse:
                draw.rectangle(
                    [
                        left,
                        top,
                        left + geometry.cell_width - 1,
                        top + geometry.cell_height - 1,
                    ],
                    fill=foreground_color,
                )
                foreground_color = background_color
            if cell.cursor:
                draw.rectangle(
                    [
                        left,
                        top,
                        left + geometry.cell_width - 1,
                        top + geometry.cell_height - 1,
                    ],
                    fill=CURSOR_BG,
                )
                foreground_color = CURSOR_FG
            chosen = _choose_font(cell.char, main=font, cjk=cjk, symbol=symbol)
            draw.text((left, top), cell.char, font=chosen, fill=foreground_color)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _same_bg(left: CellStyle, right: CellStyle) -> bool:
    """两个格是否共享同一底色（不含光标格）。"""
    return not left.cursor and not right.cursor and left.bg == right.bg


def _resolve(
    color: str, *, default: str, palette: dict[str, str], bold: bool = False
) -> str:
    """把 pyte 的颜色值映射成 ``#rrggbb``。"""
    text = color.strip().lower()
    if not text or text == "default":
        return default
    if text.startswith("#") and len(text) in (4, 7):
        return text
    bright = text.startswith(_BRIGHT_SUFFIX)
    base = text[len(_BRIGHT_SUFFIX) :] if bright else text
    value = palette.get(base)
    if value is None:
        return default
    if bright or bold:
        return _lighten(value, 0.25)
    return value


def _lighten(hex_color: str, amount: float) -> str:
    """把颜色朝白色靠一点（模拟 bright/bold）。"""
    raw = hex_color.lstrip("#")
    if len(raw) != 6:
        return hex_color
    channels = [int(raw[index : index + 2], 16) for index in (0, 2, 4)]
    lifted = [min(255, round(value + (255 - value) * amount)) for value in channels]
    return "#{:02x}{:02x}{:02x}".format(*lifted)


def font_dir() -> str:
    """字体所在目录（调试用）。"""
    return os.path.dirname(resolve_fonts("")[0] if resolve_fonts("")[0] else "")
