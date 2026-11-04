"""terminal 服务的运行时配置。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TerminalConfig"]


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
    font_path: str = Field(default="", description="渲染字体路径；空表示自动探测")
    cjk_font_path: str = Field(
        default="", description="宽字符（中日韩）回退字体；空表示自动探测"
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

    def resolve_shell(self) -> str:
        """选一个**带行编辑**的 shell。

        显式配置的 ``shell`` 原样返回（哪怕它没有 readline——那是你的选择）。
        否则依次试 ``$SHELL``、``bash``、系统 profile 的 bash、zsh、fish、sh，
        跳过 ``bind -v`` 失败的 bash（NixOS 的 ``bash`` 常常就是这种）。
        """
        text = self.shell.strip()
        if text:
            return text
        from blue_service_terminal.session import supports_line_editing

        candidates = [
            os.environ.get("SHELL", "").strip(),
            "bash",
            "/run/current-system/sw/bin/bash",
            "/usr/bin/bash",
            "zsh",
            "fish",
            "sh",
        ]
        fallback = ""
        for candidate in candidates:
            if not candidate:
                continue
            path = candidate if "/" in candidate else shutil.which(candidate) or ""
            if not path or ("/" in candidate and not Path(path).is_file()):
                continue
            if not fallback:
                fallback = candidate
            if supports_line_editing(path) is not False:
                return candidate
        return fallback or "bash"

    def resolve_workdir(self) -> Path:
        """解析工作目录（相对路径按项目根）。"""
        from blue.runtime import WorkspaceConfig, resolve_workspace

        return resolve_workspace(WorkspaceConfig(workdir=self.workdir))
