"""terminal 服务的运行时配置。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from blue_service_terminal.colors import (
    DEFAULT_BACKGROUND,
    DEFAULT_FOREGROUND,
    ansi_palette,
    normalise_color,
)

__all__ = ["PaletteColor", "TerminalConfig"]


class PaletteColor(BaseModel):
    """覆盖 ANSI 基本色的一项（``name`` 取 ``colors.ANSI_BASE`` 里的键）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="颜色名，如 red / green / brightblue 的前缀部分")
    color: str = Field(description="色值：#rgb、#rrggbb 或 rgb:rrrr/gggg/bbbb")


class TerminalConfig(BaseModel):
    """一个终端进程池的参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    shell: str = Field(
        default="",
        description="默认 shell；空表示自动挑选（会跳过没有 readline 的 bash 构建）",
    )
    workdir: str | None = Field(
        default=None, description="新会话的工作目录；缺省为项目根"
    )
    max_sessions: int = Field(default=6, ge=1, le=16, description="同时打开的会话上限")
    cols: int = Field(default=100, ge=20, le=400, description="默认列数")
    rows: int = Field(default=30, ge=5, le=200, description="默认行数")
    scrollback: int = Field(default=2000, ge=0, le=10_000, description="保留的历史行数")
    font_path: str | None = Field(
        default=None, description="渲染字体路径；null 表示自动探测"
    )
    cjk_font_path: str | None = Field(
        default=None, description="宽字符（中日韩）回退字体；null 表示自动探测"
    )
    symbol_font_path: str | None = Field(
        default=None,
        description="符号回退字体（Nerd Font）：私用区图标（nvim 状态栏、Powerline）"
        "靠它才画得出来；null 表示自动探测",
    )
    isolate_rc: bool = Field(
        default=True,
        description="不加载用户的 shell rc（bash --norc / zsh --no-rcs），"
        "让提示符与环境可预测；关掉则沿用用户配置",
    )
    prompt: str = Field(
        default=r"\u@\h:\w\$ ",
        description="会话内固定的 PS1（避免用户配色转义弄脏截图）；空表示沿用用户配置",
    )
    font_size: int = Field(default=15, ge=8, le=40, description="渲染字号（像素）")
    idle_ttl: float = Field(
        default=1800.0, gt=0, description="空闲会话的存活时长（秒）"
    )
    background: str = Field(
        default=DEFAULT_BACKGROUND,
        description="终端背景色：截图底色，同时用于应答程序的 OSC 11 查询",
    )
    foreground: str = Field(
        default=DEFAULT_FOREGROUND,
        description="默认前景色：截图默认字色，同时用于应答 OSC 10 查询",
    )
    palette: list[PaletteColor] = Field(
        default_factory=list[PaletteColor],
        description="覆盖 ANSI 基本色的一项或多项；name 取 colors.ANSI_BASE 的键",
    )
    respond_to_queries: bool = Field(
        default=True,
        description="是否应答程序的终端查询（DSR 5/6、DA1、OSC 10/11）；"
        "关掉则 nvim 之类会报 E1568",
    )

    @field_validator("background", "foreground")
    @classmethod
    def _validate_color(cls, value: str, info: ValidationInfo) -> str:
        return normalise_color(value, field=str(info.field_name))

    @field_validator("palette")
    @classmethod
    def _validate_palette(cls, value: list[PaletteColor]) -> list[PaletteColor]:
        overrides = {entry.name: entry.color for entry in value}
        ansi_palette(overrides)  # 键名与色值都由它校验，非法直接抛 ValueError
        return [
            PaletteColor(
                name=entry.name.strip().lower(),
                color=normalise_color(entry.color, field=f"palette.{entry.name}"),
            )
            for entry in value
        ]
