"""blue 外置工具：用无头 Firefox 给网页拍截图。

工具把 PNG 作为**图片附件**返回（``blue.runtime.tool_attachment``），因此 agent 能
真的「看见」页面，而不是只看一段文字描述——这正是多模态工具结果那条链路。

为什么是无头 Firefox：这台机器上没有 node/chromium，Firefox 的
``--headless --screenshot`` 是唯一零依赖就能出图的方案。代价是**只能截图，不能交互**
（点击/输入需要 geckodriver + WebDriver，留给下一阶段）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import os
import signal
import tempfile
from pathlib import Path
from typing import Annotated

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from blue.errors import BlueError
from blue.runtime import load_runtime_config, run_tool

__all__ = [
    "CONFIG_MODEL",
    "BrowserConfig",
    "BrowserShot",
    "capture",
]

JPEG_QUALITY = 85
"""超过体积上限时转 JPEG 的质量。"""

PNG_BUDGET = 1_500_000
"""PNG 超过这个字节数就转 JPEG（会话端的单附件上限是 2 MiB）。"""


class BrowserConfig(BaseModel):
    """browser 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    firefox_bin: str = Field(default="firefox", description="Firefox 可执行文件")
    timeout: float = Field(default=60.0, gt=0, description="单次截图超时（秒）")
    default_width: int = Field(default=1280, ge=320, le=3840)
    default_height: int = Field(default=900, ge=240, le=2160)
    max_edge: int = Field(default=1568, ge=320, le=4096, description="输出图片长边上限")


class BrowserShot(BaseModel):
    """截图结果：元信息 + 图片附件。"""

    url: str = Field(description="实际打开的地址")
    width: int = Field(description="输出图片宽度（像素）")
    height: int = Field(description="输出图片高度（像素）")
    size: int = Field(description="输出图片字节数")
    mime: str = Field(description="输出图片 MIME 类型")
    attachments: list[dict[str, str]] = Field(
        default_factory=list[dict[str, str]], description="图片附件（供 agent 看图）"
    )


_config: BrowserConfig = BrowserConfig()
"""进程级配置；由 :func:`configure` 在启动时注入。"""


def configure(config: BrowserConfig) -> None:
    """设置本进程后续截图使用的配置。"""
    global _config
    _config = config


async def capture(
    url: Annotated[str, "要截图的网页地址（http/https）"],
    width: Annotated[int, Field(ge=320, le=3840), "视口宽度"] = 0,
    height: Annotated[int, Field(ge=240, le=2160), "视口高度"] = 0,
) -> BrowserShot:
    """用无头 Firefox 给网页拍一张截图，并以图片附件返回。

    截图范围等于视口大小（Firefox 的 ``--screenshot`` 不支持整页）；
    超过体积上限时会等比缩小并转成 JPEG，保证能被会话层的附件上限接受。

    :param url: 目标地址，仅支持 http/https
    :param width: 视口宽度；``0`` 表示用配置里的默认值
    :param height: 视口高度；``0`` 表示用配置里的默认值
    :return: 截图元信息与图片附件
    :raises ValueError: 地址非法、Firefox 失败或超时
    """
    target = url.strip()
    if not target:
        raise ValueError("url 不能为空")
    if not target.startswith(("http://", "https://")):
        raise ValueError("只支持 http/https 地址")
    viewport_w = width or _config.default_width
    viewport_h = height or _config.default_height

    png = await _firefox_screenshot(target, viewport_w, viewport_h)
    data, mime, out_w, out_h = _normalize(png, max_edge=_config.max_edge)
    return BrowserShot(
        url=target,
        width=out_w,
        height=out_h,
        size=len(data),
        mime=mime,
        attachments=[{"mime": mime, "data": base64.b64encode(data).decode("ascii")}],
    )


async def _firefox_screenshot(url: str, width: int, height: int) -> bytes:
    """调用无头 Firefox 抓图，返回 PNG 字节。"""
    with tempfile.TemporaryDirectory(prefix="blue-browser-") as workdir:
        shot = Path(workdir) / "shot.png"
        profile = Path(workdir) / "profile"
        profile.mkdir()
        environment = dict(os.environ)
        # 用独立 HOME/配置目录，避免和用户正在跑的 Firefox 抢 profile
        environment["HOME"] = workdir
        command = [
            _config.firefox_bin,
            "--headless",
            "-no-remote",
            "--profile",
            str(profile),
            "--window-size",
            f"{width},{height}",
            "--screenshot",
            str(shot),
            url,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_config.timeout
            )
        except TimeoutError:
            _kill(process)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5.0)
            raise ValueError(f"截图超时（{_config.timeout:g}s）：{url}") from None
        if process.returncode != 0 or not shot.is_file():
            detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
            hint = detail[-1] if detail else f"退出码 {process.returncode}"
            raise ValueError(f"Firefox 截图失败：{hint}")
        return shot.read_bytes()


def _normalize(png: bytes, *, max_edge: int) -> tuple[bytes, str, int, int]:
    """把截图裁剪到体积预算内：等比缩放，必要时转 JPEG。"""
    with Image.open(io.BytesIO(png)) as raw:
        image = raw.convert("RGB")
        if max(image.width, image.height) > max_edge:
            # thumbnail 原地等比缩放，避免依赖 Pillow 的 resize 返回类型标注
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if len(png) <= PNG_BUDGET:
            image.save(buffer, format="PNG", optimize=True)
            mime = "image/png"
        else:
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            mime = "image/jpeg"
        return buffer.getvalue(), mime, image.width, image.height


def _kill(process: asyncio.subprocess.Process) -> None:
    """杀掉 Firefox 及其子进程。"""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()


CONFIG_MODEL = BrowserConfig
"""契约测试使用的运行时配置模型。"""

TOOL = capture
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    try:
        configure(load_runtime_config(BrowserConfig))
    except BlueError:
        # 没被 supervisor 托管时（手工运行）用默认配置
        configure(BrowserConfig())
    return run_tool(TOOL, "browser_screenshot")


if __name__ == "__main__":
    raise SystemExit(main())
