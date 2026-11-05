"""blue 外置工具：写入文本文件。"""

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
    "WriteFileResult",
    "main",
    "write_file",
]

CONFIG_MODEL = WorkspaceConfig
"""契约测试使用的运行时配置模型。"""


class WriteFileResult(BaseModel):
    """文件写入结果。"""

    path: str = Field(description="解析后的绝对路径")
    bytes_written: int = Field(description="写入的 UTF-8 字节数")
    created: bool = Field(description="写入前文件是否不存在")


def write_file(
    path: Annotated[str, "文件路径，支持相对路径、绝对路径与 ~"],
    content: Annotated[str, "要写入的完整文本"],
    create_dirs: Annotated[bool, "父目录不存在时是否自动创建"] = True,
) -> WriteFileResult:
    """用给定内容覆盖写入 UTF-8 文本文件。

    相对路径按服务配置的 ``workdir`` 解析，缺省为项目根（``blue.infd`` 所在目录）。
    """
    target = workspace().resolve(path)
    if target.is_dir():
        raise ValueError(f"目标路径是目录：{target}")

    created = not target.exists()
    parent = target.parent
    if not parent.is_dir():
        if not create_dirs:
            raise ValueError(f"父目录不存在：{parent}")
        parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return WriteFileResult(
        path=str(target),
        bytes_written=len(content.encode("utf-8")),
        created=created,
    )


TOOL = write_file
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure_workspace(load_runtime_config(CONFIG_MODEL))
    return run_tool(TOOL, "write_file")


if __name__ == "__main__":
    raise SystemExit(main())
