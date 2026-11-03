"""外置工具处理器：文本统计。"""

from typing import Annotated

from pydantic import BaseModel, Field

from handlers.support import run_tool


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


if __name__ == "__main__":
    raise SystemExit(run_tool(TOOL, "text_stats"))
