"""blue 外置工具：文本统计。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from blue.runtime import run_tool


class TextStats(BaseModel):
    """文本统计结果。"""

    characters: int = Field(description="字符数（含空白）")
    words: int = Field(description="按空白切分的词数")
    lines: int = Field(description="行数")


def text_stats(text: Annotated[str, "待统计的文本"]) -> TextStats:
    """统计文本的字符数、词数与行数。"""
    return TextStats(
        characters=len(text),
        words=len(text.split()),
        lines=len(text.splitlines()),
    )


TOOL = text_stats
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    return run_tool(TOOL, "text_stats")


if __name__ == "__main__":
    raise SystemExit(main())
