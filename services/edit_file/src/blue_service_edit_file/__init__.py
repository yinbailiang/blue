"""blue 外置工具：精确替换文本文件内容。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from blue.runtime import (
    WorkspaceConfig,
    configure_workspace,
    load_runtime_config,
    workspace,
)
from blue_tool_sdk import run_tool

__all__ = [
    "CONFIG_MODEL",
    "EditFileResult",
    "edit_file",
    "main",
]

CONFIG_MODEL = WorkspaceConfig
"""契约测试使用的运行时配置模型。"""


class EditFileResult(BaseModel):
    """文件编辑结果。"""

    path: str = Field(description="解析后的绝对路径")
    replacements: int = Field(description="实际替换的处数")
    bytes_written: int = Field(description="写入的 UTF-8 字节数")


def edit_file(
    path: Annotated[str, "文件路径，支持相对路径、绝对路径与 ~"],
    old_text: Annotated[str, "要替换的原文，必须与文件中内容精确匹配"],
    new_text: Annotated[str, "替换后的文本"],
    replace_all: Annotated[bool, "是否替换全部匹配；默认要求原文唯一"] = False,
) -> EditFileResult:
    """在 UTF-8 文本文件中做精确字符串替换，并保留原换行风格。

    相对路径按服务配置的 ``workdir`` 解析，缺省为项目根（``blue.infd`` 所在目录）。
    """
    target = workspace().resolve(path)
    if not target.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{target}")
    if not old_text:
        raise ValueError("old_text 不能为空")

    with target.open("r", encoding="utf-8", newline="") as handle:
        original = handle.read()

    newline = "\r\n" if "\r\n" in original else "\n"
    new_text = _with_newline(new_text, newline)

    occurrences = original.count(old_text)
    if occurrences == 0:
        old_text = _with_newline(old_text, newline)
        occurrences = original.count(old_text)
        if occurrences == 0:
            raise ValueError("未找到要替换的原文；请先用 read_file 查看当前内容")

    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"原文匹配到 {occurrences} 处；请补充上下文，或设置 replace_all=true"
        )

    updated = original.replace(old_text, new_text, -1 if replace_all else 1)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)

    return EditFileResult(
        path=str(target),
        replacements=occurrences if replace_all else 1,
        bytes_written=len(updated.encode("utf-8")),
    )


def _with_newline(text: str, newline: str) -> str:
    """把文本统一为指定换行风格。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


TOOL = edit_file
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure_workspace(load_runtime_config(CONFIG_MODEL))
    return run_tool(TOOL, "edit_file")


if __name__ == "__main__":
    raise SystemExit(main())
