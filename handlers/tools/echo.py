"""外置工具处理器：回显文本。"""

from typing import Annotated

from pydantic import BaseModel, Field

from handlers.support import run_tool


class EchoResult(BaseModel):
    """回显结果。"""

    text: str = Field(description="原文")
    length: int = Field(description="字符数")


def echo(text: Annotated[str, "要回显的文本"]) -> EchoResult:
    """原样回显输入文本，并给出字符数。"""
    return EchoResult(text=text, length=len(text))


TOOL = echo
"""工具进程入口。"""


if __name__ == "__main__":
    raise SystemExit(run_tool(TOOL, "echo"))
