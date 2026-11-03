"""blue 外置工具：读取文本文件。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from blue.runtime import (
    WorkspaceConfig,
    configure_workspace,
    load_runtime_config,
    run_tool,
    workspace,
)

__all__ = [
    "CONFIG_MODEL",
    "DEFAULT_MAX_LINES",
    "ReadFileResult",
    "main",
    "read_file",
]

CONFIG_MODEL = WorkspaceConfig
"""契约测试使用的运行时配置模型。"""

DEFAULT_MAX_LINES = 2000
"""单次读取的默认行数上限。"""


class ReadFileResult(BaseModel):
    """文件读取结果。"""

    path: str = Field(description="解析后的绝对路径")
    content: str = Field(description="读取到的文本，按行以 \\n 连接")
    total_lines: int = Field(description="文件总行数")
    start_line: int = Field(description="本次读取的起始行号（1 起，含）")
    end_line: int = Field(description="本次读取的结束行号（含）；空文件为 0")
    truncated: bool = Field(description="是否因 max_lines 截断")


def read_file(
    path: Annotated[str, "文件路径，支持相对路径、绝对路径与 ~"],
    start_line: Annotated[int, Field(ge=1), "起始行号（1 起，含）"] = 1,
    max_lines: Annotated[int, Field(ge=1), "最多读取的行数"] = DEFAULT_MAX_LINES,
) -> ReadFileResult:
    """按行范围读取 UTF-8 文本文件。

    相对路径按服务配置的 ``workdir`` 解析，缺省为项目根（``blue.infd`` 所在目录）。
    """
    target = workspace().resolve(path)
    if not target.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{target}")

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    if total > 0 and start_line > total:
        raise ValueError(f"起始行号 {start_line} 超出文件总行数 {total}")

    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    end_line = start_line + len(selected) - 1 if selected else 0
    return ReadFileResult(
        path=str(target),
        content="\n".join(selected),
        total_lines=total,
        start_line=start_line,
        end_line=end_line,
        truncated=end_line < total,
    )


TOOL = read_file
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure_workspace(load_runtime_config(CONFIG_MODEL))
    return run_tool(TOOL, "read_file")


if __name__ == "__main__":
    raise SystemExit(main())
