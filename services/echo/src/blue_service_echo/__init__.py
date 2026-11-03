"""blue 外置工具：回显文本。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from blue.runtime import run_tool


class EchoResult(BaseModel):
    """回显结果。"""

    text: str = Field(description="原文")
    length: int = Field(description="字符数")


def echo(text: Annotated[str, "要回显的文本"]) -> EchoResult:
    """原样回显输入文本，并给出字符数。"""
    return EchoResult(text=text, length=len(text))


TOOL = echo
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    return run_tool(TOOL, "echo")


if __name__ == "__main__":
    raise SystemExit(main())
