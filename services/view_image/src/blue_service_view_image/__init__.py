"""blue 外置工具：把本地图片「给模型看」。

这是 ``browser`` 的互补工具：``browser`` 把网页变成图，``view_image`` 把磁盘上的图
（截图、生成的图表、下载的图片……）作为**图片附件**交给模型，让 agent 真的能看图，
而不是只读到「文件存在，12345 字节」。

相对路径与 ``read_file`` / ``shell`` 同一套语义：锚定项目根（``BLUE_PROJECT_ROOT``），
由 ``workdir`` 覆盖。
"""

from __future__ import annotations

import base64
import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from blue.errors import BlueError
from blue.images import normalize_image
from blue.runtime import (
    WorkspaceConfig,
    configure_workspace,
    load_runtime_config,
    run_tool,
    workspace,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_MODEL",
    "ViewImageConfig",
    "ViewImageResult",
    "main",
    "view_image",
]


class ViewImageConfig(BaseModel):
    """view_image 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workdir: str | None = Field(
        default=None,
        description="相对路径的锚点；缺省为项目根，绝对路径不受影响",
    )
    max_edge: int = Field(default=1568, ge=320, le=4096, description="输出图片长边上限")
    max_bytes: int = Field(
        default=33_554_432, ge=1024, description="源文件体积上限（默认 32 MiB）"
    )


class ViewImageResult(BaseModel):
    """图片查看结果：元信息 + 图片附件。"""

    path: str = Field(description="解析后的绝对路径")
    width: int = Field(description="输出图片宽度（像素）")
    height: int = Field(description="输出图片高度（像素）")
    size: int = Field(description="输出图片字节数")
    mime: str = Field(description="输出图片 MIME 类型")
    attachments: list[dict[str, str]] = Field(
        default_factory=list[dict[str, str]], description="图片附件（供 agent 看图）"
    )


_base: ViewImageConfig = ViewImageConfig()
"""进程级配置；由 :func:`configure` 在启动时注入。"""


def configure(config: ViewImageConfig) -> None:
    """设置本进程后续查看图片使用的配置。"""
    global _base
    _base = config
    configure_workspace(WorkspaceConfig(workdir=config.workdir))


def view_image(
    path: Annotated[str, "图片路径；相对路径锚定项目根"],
) -> ViewImageResult:
    """读取一张本地图片并以图片附件返回（模型可以直接「看见」它）。

    仅支持 JPEG / PNG / GIF / WebP（按文件内容嗅探，不看扩展名）；动图只取首帧。
    长边超过上限会等比缩小，PNG 过大会转 JPEG，保证落在会话层的附件预算内。

    :param path: 图片路径；相对路径按项目根（或配置的 ``workdir``）解析
    :return: 元信息与图片附件
    :raises ValueError: 路径为空、文件不存在、超过体积上限或不是支持的图片
    """
    target = workspace().resolve(path)
    if not target.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{target}")
    if target.stat().st_size > _base.max_bytes:
        raise ValueError(
            f"图片过大（{target.stat().st_size} > {_base.max_bytes} 字节）：{target}"
        )
    data = target.read_bytes()
    payload = normalize_image(data, max_edge=_base.max_edge)
    logger.debug(
        "查看图片 %s：%s %sx%s → %s %sx%s",
        target,
        len(data),
        "?",
        "?",
        payload.mime,
        payload.width,
        payload.height,
    )
    return ViewImageResult(
        path=str(target),
        width=payload.width,
        height=payload.height,
        size=len(payload.data),
        mime=payload.mime,
        attachments=[
            {
                "mime": payload.mime,
                "data": base64.b64encode(payload.data).decode("ascii"),
            }
        ],
    )


CONFIG_MODEL = ViewImageConfig
"""契约测试使用的运行时配置模型。"""

TOOL = view_image
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    try:
        configure(load_runtime_config(ViewImageConfig))
    except BlueError:
        configure(ViewImageConfig())
    return run_tool(TOOL, "view_image")


if __name__ == "__main__":
    raise SystemExit(main())
