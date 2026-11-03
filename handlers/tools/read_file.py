"""外置工具处理器：读取文本文件。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from handlers.support import run_tool

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
    """按行范围读取 UTF-8 文本文件。"""
    target = _resolve_path(path)
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


def _resolve_path(path: str) -> Path:
    """把用户路径解析为绝对路径。"""
    return Path(path).expanduser().resolve()


TOOL = read_file
"""工具进程入口。"""


if __name__ == "__main__":
    raise SystemExit(run_tool(TOOL, "read_file"))
