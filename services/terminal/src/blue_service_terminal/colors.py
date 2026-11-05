"""终端颜色的解析与转换。

同一个颜色值要在三处用：配置（人写的 ``#rrggbb``）、截图（PIL 要 ``#rrggbb``）、
查询应答（xterm 的 OSC 10/11 要求 ``rgb:rrrr/gggg/bbbb``）。集中在这里转换，
免得三个模块各写一套正则、格式还互相不一致。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

__all__ = [
    "ANSI_BASE",
    "DEFAULT_BACKGROUND",
    "DEFAULT_FOREGROUND",
    "ansi_palette",
    "normalise_color",
    "to_osc_rgb",
]

DEFAULT_BACKGROUND: Final = "#0b1020"
"""默认背景色（暗色主题）。"""

DEFAULT_FOREGROUND: Final = "#e5e7eb"
"""默认前景色。"""

ANSI_BASE: Final[dict[str, str]] = {
    "black": "#1f2430",
    "red": "#f07178",
    "green": "#a5d6a7",
    "brown": "#d7ba7d",
    "yellow": "#e5c07b",
    "blue": "#82aaff",
    "magenta": "#c792ea",
    "cyan": "#89ddff",
    "white": "#d7dae0",
}
"""ANSI 基本色（暗色主题下取了较柔和的色值）。"""

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_OSC_RGB = re.compile(
    r"^rgb:\s*([0-9a-fA-F]{1,4})\s*/\s*([0-9a-fA-F]{1,4})\s*/\s*([0-9a-fA-F]{1,4})$",
    re.IGNORECASE,
)


def normalise_color(value: str, *, field: str = "颜色") -> str:
    """把任意受支持的颜色写法统一成 ``#rrggbb``。

    接受 ``#rgb``、``rrggbb``、``rgb:RRRR/GGGG/BBBB``（xterm 查询里就是最后这种）。

    :param value: 原始颜色文本
    :param field: 出错时用于提示的字段名
    :return: ``#rrggbb`` 形式的小写十六进制颜色
    :raises ValueError: 无法识别的写法
    """
    text = value.strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    hex_match = _HEX.match(text)
    if hex_match is not None:
        digits = hex_match.group(1).lower()
        if len(digits) == 3:
            # #abc → #aabbcc：每通道复制一位
            digits = "".join(ch * 2 for ch in digits)
        return f"#{digits}"
    rgb_match = _OSC_RGB.match(text)
    if rgb_match is not None:
        channels: list[int] = []
        for part in rgb_match.groups():
            raw = int(part, 16)
            # xterm 的每通道是 16 位，按最大位宽缩放到 8 位
            channels.append(round(raw * 255 / (16 ** len(part) - 1)))
        return "#{:02x}{:02x}{:02x}".format(*channels)
    raise ValueError(
        f"{field}格式无法识别：{value!r}（支持 #rgb、#rrggbb、rgb:rrrr/gggg/bbbb）"
    )


def to_osc_rgb(value: str, *, field: str = "颜色") -> str:
    """把颜色转成 OSC 10/11 应答里要的 ``rgb:RRRR/GGGG/BBBB``。

    每通道 16 位：8 位值重复一次（``#0b`` → ``0b0b``），这是 xterm 的惯例。

    :param value: 任意受支持的颜色写法
    :param field: 出错时用于提示的字段名
    :return: ``rgb:rrrr/gggg/bbbb``
    """
    normalised = normalise_color(value, field=field)
    raw = normalised.lstrip("#")
    channels = [raw[index : index + 2] for index in (0, 2, 4)]
    return "rgb:" + "/".join(ch * 2 for ch in channels)


def ansi_palette(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """内置 ANSI 调色板，叠加用户覆盖项。

    :param overrides: 形如 ``{"red": "#ff5555"}`` 的覆盖值；键须是调色板里已有的名字
    :return: 合并后的调色板
    :raises ValueError: 覆盖项里出现未知的颜色名或非法色值
    """
    palette = dict(ANSI_BASE)
    if not overrides:
        return palette
    for name, value in overrides.items():
        key = name.strip().lower()
        if key not in ANSI_BASE:
            raise ValueError(
                f"未知的调色板颜色名：{name!r}（可用：{', '.join(sorted(ANSI_BASE))}）"
            )
        palette[key] = normalise_color(value, field=f"palette.{key}")
    return palette
