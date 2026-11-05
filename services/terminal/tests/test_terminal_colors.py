"""终端颜色：写法归一、OSC 应答格式、配置校验与截图配色。"""

from __future__ import annotations

import io

import pytest
from blue_service_terminal.colors import (
    ANSI_BASE,
    DEFAULT_BACKGROUND,
    DEFAULT_FOREGROUND,
    ansi_palette,
    normalise_color,
    to_osc_rgb,
)
from blue_service_terminal.config import PaletteColor, TerminalConfig
from blue_service_terminal.render import (
    FontSet,
    FrameGeometry,
    geometry_for,
    render_screen,
    resolve_fonts,
)
from blue_service_terminal.session import CellStyle
from PIL import Image
from pydantic import ValidationError


def test_normalise_color_accepts_common_notations() -> None:
    """三种写法都能归一成 #rrggbb。"""
    assert normalise_color("#0b1020") == "#0b1020"
    assert normalise_color("0B1020") == "#0b1020"
    assert normalise_color("#abc") == "#aabbcc"
    assert normalise_color("rgb:0b0b/1010/2020") == "#0b1020"
    assert normalise_color("rgb:ffff/0000/8080") == "#ff0080"


def test_normalise_color_rejects_garbage() -> None:
    """认不出的写法必须报错，不能悄悄回退成默认色。"""
    for bad in ("", "   ", "#12345", "rgb:1/2", "#gggggg", "浅蓝"):
        with pytest.raises(ValueError):
            normalise_color(bad)


def test_to_osc_rgb_duplicates_each_byte() -> None:
    """xterm 要 16 位每通道：8 位值重复一次。"""
    assert to_osc_rgb("#0b1020") == "rgb:0b0b/1010/2020"
    assert to_osc_rgb("#fff") == "rgb:ffff/ffff/ffff"


def test_ansi_palette_overrides_and_validates() -> None:
    """覆盖只改指定的那几项，未知颜色名与非色值都要报错。"""
    palette = ansi_palette()

    assert palette["red"] == ANSI_BASE["red"]
    assert "default" not in palette

    merged = ansi_palette({"red": "#FF5555", "blue": "#00f"})

    assert merged["red"] == "#ff5555"
    assert merged["blue"] == "#0000ff"
    assert merged["green"] == ANSI_BASE["green"]

    with pytest.raises(ValueError):
        ansi_palette({"nonexistent": "#fff"})
    with pytest.raises(ValueError):
        ansi_palette({"red": "not-a-color"})


def test_config_defaults_match_colors_module() -> None:
    """配置默认值不能和渲染层的默认色漂移。"""
    config = TerminalConfig()

    assert config.background == DEFAULT_BACKGROUND
    assert config.foreground == DEFAULT_FOREGROUND
    assert config.palette == []
    assert config.respond_to_queries is True


def test_config_normalises_colors_and_palette() -> None:
    """配置层负责把写法归一，下游拿到的永远是 #rrggbb。"""
    config = TerminalConfig(
        background="#111",
        foreground="rgb:ffff/ffff/ffff",
        palette=[PaletteColor(name="Red", color="#F00")],
    )

    assert config.background == "#111111"
    assert config.foreground == "#ffffff"
    assert config.palette_map() == {"red": "#ff0000"}


def test_config_rejects_bad_colors() -> None:
    """非法色值和未知调色板名都要在配置阶段就挡住。"""
    with pytest.raises(ValidationError):
        TerminalConfig(background="不是颜色")
    with pytest.raises(ValidationError):
        TerminalConfig(palette=[PaletteColor(name="nope", color="#fff")])
    with pytest.raises(ValidationError):
        TerminalConfig(palette=[PaletteColor(name="red", color="not-a-color")])


def _render(
    *,
    cols: int = 4,
    rows: int = 2,
    cells: list[list[CellStyle]] | None = None,
    background: str = DEFAULT_BACKGROUND,
    palette: dict[str, str] | None = None,
) -> tuple[Image.Image, FrameGeometry]:
    """渲染一张小图，返回图像与几何（用于按格子取像素）。"""
    main_path, cjk_path = resolve_fonts("", "")
    fonts = FontSet(main_path=main_path, cjk_path=cjk_path, size=15)
    geometry = geometry_for(cols=cols, rows=rows, font_path=main_path, font_size=15)
    blank = [[CellStyle(char=" ") for _ in range(cols)] for _ in range(rows)]
    content = cells if cells is not None else blank
    png = render_screen(
        content, geometry, fonts=fonts, background=background, palette=palette
    )
    return Image.open(io.BytesIO(png)).convert("RGB"), geometry


def test_render_uses_configured_background() -> None:
    """背景色配置要真的画进图里（左上角 padding 区一定是底色）。"""
    image, _ = _render(background="#123456")

    assert image.getpixel((0, 0)) == (0x12, 0x34, 0x56)


def test_render_palette_override_changes_cell_background() -> None:
    """palette 覆盖后，ANSI 名为 red 的底色要变成新色值。"""
    cells = [[CellStyle(char=" ", bg="red") for _ in range(4)] for _ in range(2)]

    default_image, geometry = _render(cells=cells)
    overridden, _ = _render(cells=cells, palette={"red": "#00ff00"})

    x, y = geometry.to_pixel(1, 1)  # 空格没有字形，格子中心就是底色
    assert default_image.getpixel((x, y)) == (0xF0, 0x71, 0x78)
    assert overridden.getpixel((x, y)) == (0x00, 0xFF, 0x00)
