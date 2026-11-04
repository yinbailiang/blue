"""blue 外置工具：可交互的浏览器（WebDriver）。

``browser`` 服务用无头 Firefox 的 ``--screenshot`` 只能「打开就拍」，SPA 的异步内容
往往还没渲染完（实测 WebUI 会拍到「离线 / 工具 0」）。本服务改用 **WebDriver
（geckodriver）**，因此可以：

- 等某个选择器出现（``wait_for``）再截图；
- 按顺序执行动作：点击 / 输入 / 按键 / 滚动 / 等待 / 执行 JS / 跳转；
- 截图仍然是**图片附件**，agent 直接看见页面；
- 同时回传标题与页面可见文本，便于在无图时也能判断状态。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import signal
import socket
import time
from typing import Annotated, Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from blue.errors import BlueError
from blue.images import normalize_image
from blue.runtime import load_runtime_config, run_tool

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_MODEL",
    "BrowserAction",
    "BrowserConfig",
    "BrowserOutcome",
    "browser_operate",
    "main",
]

TEXT_LIMIT = 1500
"""回传给模型的正文字符上限。"""

DRIVER_BOOT_TIMEOUT = 25.0
"""geckodriver 启动等待上限。"""


class BrowserConfig(BaseModel):
    """browser_ops 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver_bin: str = Field(default="geckodriver", description="geckodriver 可执行文件")
    browser_bin: str | None = Field(
        default=None, description="Firefox 路径；None 交给 driver"
    )
    timeout: float = Field(default=90.0, gt=0, description="单次操作的超时（秒）")
    default_width: int = Field(default=1280, ge=320, le=3840)
    default_height: int = Field(default=900, ge=240, le=2160)
    max_edge: int = Field(default=1568, ge=320, le=4096)


class BrowserAction(BaseModel):
    """一个浏览器动作。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["click", "type", "press", "scroll", "wait", "navigate", "js"] = Field(
        description="动作类型"
    )
    selector: str = Field(default="", description="CSS 选择器（click/type 用）")
    text: str = Field(default="", description="输入文本（type 用）或 JS 代码（js 用）")
    key: str = Field(default="", description="按键名（press 用），如 Enter、Escape")
    ms: int = Field(default=0, ge=0, le=60000, description="等待毫秒（wait 用）")
    value: str = Field(
        default="", description="跳转地址（navigate 用）或滚动像素（scroll 用）"
    )


class BrowserOutcome(BaseModel):
    """一次浏览器操作的结果。"""

    url: str = Field(description="结束时的地址")
    title: str = Field(default="", description="页面标题")
    text: str = Field(default="", description="页面可见文本（截断）")
    steps: list[str] = Field(
        default_factory=list[str], description="已执行的动作与结果"
    )
    width: int = Field(default=0, description="截图宽度")
    height: int = Field(default=0, description="截图高度")
    size: int = Field(default=0, description="截图字节数")
    mime: str = Field(default="", description="截图 MIME")
    attachments: list[dict[str, str]] = Field(
        default_factory=list[dict[str, str]], description="截图附件"
    )


_config: BrowserConfig = BrowserConfig()
"""进程级配置；由 :func:`configure` 在启动时注入。"""


def configure(config: BrowserConfig) -> None:
    """设置本进程后续操作使用的配置。"""
    global _config
    _config = config


async def browser_operate(
    url: Annotated[str, "起始地址（http/https；动作里还能再 navigate）"],
    actions: Annotated[
        list[BrowserAction], "按顺序执行的动作；为空表示只打开并截图"
    ] = [],  # noqa: B006 - 工具签名要求可变默认值由框架序列化
    width: Annotated[int, Field(ge=320, le=3840), "视口宽度（0=默认）"] = 0,
    height: Annotated[int, Field(ge=240, le=2160), "视口高度（0=默认）"] = 0,
    wait_for: Annotated[str, "截图前等待出现的 CSS 选择器（空=不等待）"] = "",
    wait_js: Annotated[str, "截图前轮询的 JS 表达式；返回真值即继续（空=不等待）"] = "",
    wait_timeout: Annotated[float, Field(gt=0, le=120), "等待秒数"] = 10.0,
) -> BrowserOutcome:
    """用 WebDriver 打开页面、执行动作并截图（截图作为图片附件返回）。

    可以点击、输入、按键、滚动、等待、执行 JS、跳转；只支持 http/https。
    """
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        raise ValueError("只支持 http/https 地址")
    viewport_w = width or _config.default_width
    viewport_h = height or _config.default_height

    driver = _Driver(_config)
    try:
        await driver.start(viewport_w, viewport_h)
        await driver.navigate(target)
        steps: list[str] = [f"navigate {target}"]
        if wait_for.strip():
            ok = await driver.wait_for_selector(wait_for.strip(), wait_timeout)
            steps.append(f"wait_for {wait_for.strip()} → {'ok' if ok else 'timeout'}")
        if wait_js.strip():
            ok = await driver.wait_for_js(wait_js.strip(), wait_timeout)
            steps.append(
                f"wait_js {wait_js.strip()[:40]} → {'ok' if ok else 'timeout'}"
            )
        for action in actions:
            steps.append(await driver.perform(action))
        title = await driver.title()
        text = await driver.body_text(TEXT_LIMIT)
        png = await driver.screenshot()
        final_url = await driver.current_url()
    finally:
        await driver.stop()

    payload = normalize_image(png, max_edge=_config.max_edge)
    return BrowserOutcome(
        url=final_url,
        title=title,
        text=text,
        steps=steps,
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


class _Driver:
    """最小的 WebDriver 客户端（只覆盖本工具需要的端点）。"""

    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._base = ""
        self._session = ""

    async def start(self, width: int, height: int) -> None:
        """拉起 geckodriver 并建立会话。"""
        port = _free_port()
        self._base = f"http://127.0.0.1:{port}"
        environment = dict(os.environ)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._config.driver_bin,
                "--port",
                str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        except FileNotFoundError as error:
            raise ValueError(
                f"找不到 geckodriver（{self._config.driver_bin}）：请先安装它"
            ) from error
        self._client = httpx.AsyncClient(timeout=self._config.timeout)
        deadline = time.monotonic() + DRIVER_BOOT_TIMEOUT
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                await self._client.post(f"{self._base}/status")
                break
            await asyncio.sleep(0.2)
        else:
            raise ValueError("geckodriver 启动超时")

        firefox_args = ["-headless", f"--width={width}", f"--height={height}"]
        options: dict[str, Any] = {"args": firefox_args}
        if self._config.browser_bin:
            options["binary"] = self._config.browser_bin
        payload = {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "firefox",
                    "moz:firefoxOptions": options,
                }
            }
        }
        response = await self._client.post(f"{self._base}/session", json=payload)
        data = _value(response)
        session = data.get("sessionId")
        if not isinstance(session, str) or not session:
            raise ValueError("WebDriver 未能建立会话")
        self._session = session

    async def navigate(self, url: str) -> None:
        """打开地址。"""
        await self._post("url", {"url": url})

    async def wait_for_selector(self, selector: str, timeout: float) -> bool:
        """轮询等待选择器出现。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self._post_raw(
                "elements", {"using": "css selector", "value": selector}
            )
            if isinstance(value, list) and value:
                return True
            await asyncio.sleep(0.25)
        return False

    async def wait_for_js(self, script: str, timeout: float) -> bool:
        """轮询一个 JS 表达式，直到返回真值或超时。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self._post_raw(
                "execute/sync", {"script": f"return ({script});", "args": []}
            )
            if value:
                return True
            await asyncio.sleep(0.25)
        return False

    async def perform(self, action: BrowserAction) -> str:
        """执行一个动作，返回人类可读的结果。"""
        kind = action.type
        if kind == "navigate":
            await self.navigate(action.value)
            return f"navigate {action.value}"
        if kind == "wait":
            await asyncio.sleep(action.ms / 1000.0)
            return f"wait {action.ms}ms"
        if kind == "js":
            value = await self._post_raw(
                "execute/sync", {"script": action.text, "args": []}
            )
            return f"js → {jsonable(value)}"
        if kind == "click":
            element = await self._element(action.selector)
            await self._post(f"element/{element}/click", {}, soft=True)
            return f"click {action.selector}"
        if kind == "type":
            element = await self._element(action.selector)
            await self._post(
                f"element/{element}/value", {"text": action.text}, soft=True
            )
            return f"type {action.selector} ({len(action.text)} 字符)"
        if kind == "press":
            element = await self._element("body")
            await self._post(
                f"element/{element}/value",
                {"text": action.key or "Enter"},
                soft=True,
            )
            return f"press {action.key or 'Enter'}"
        if kind == "scroll":
            dy = action.value or "600"
            await self._post(
                "execute/sync",
                {"script": f"window.scrollBy(0, {int(float(dy))});", "args": []},
                soft=True,
            )
            return f"scroll {dy}px"
        raise ValueError(f"不支持的动作：{kind}")

    async def title(self) -> str:
        """页面标题。"""
        return await self._get_str("title")

    async def current_url(self) -> str:
        """当前地址。"""
        return await self._get_str("url")

    async def _get_str(self, path: str) -> str:
        """GET 一个返回字符串的端点。"""
        assert self._client is not None
        try:
            response = await self._client.get(
                f"{self._base}/session/{self._session}/{path}"
            )
            if response.status_code >= 400:
                return ""
            value = _raw_value(response)
        except httpx.HTTPError, ValueError:
            return ""
        return value if isinstance(value, str) else ""

    async def body_text(self, limit: int) -> str:
        """可见文本（截断）。"""
        value = await self._post_raw(
            "execute/sync",
            {
                "script": "return document.body ? document.body.innerText : '';",
                "args": [],
            },
        )
        text = value if isinstance(value, str) else ""
        return text[:limit]

    async def screenshot(self) -> bytes:
        """整屏截图（PNG 字节）。"""
        assert self._client is not None
        response = await self._client.get(
            f"{self._base}/session/{self._session}/screenshot"
        )
        encoded = _raw_value(response)
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("WebDriver 没有返回截图")
        return base64.b64decode(encoded)

    async def stop(self) -> None:
        """关闭会话与 driver 进程（可重入）。"""
        if self._client is not None and self._session:
            with contextlib.suppress(Exception):
                await self._client.delete(f"{self._base}/session/{self._session}")
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5.0)

    async def _element(self, selector: str) -> str:
        """按 CSS 选择器取元素 id。"""
        if not selector.strip():
            raise ValueError("该动作需要 selector")
        value = await self._post(
            "element", {"using": "css selector", "value": selector}
        )
        identifier = next(iter(cast(dict[str, str], value or {}).values()), "")
        if not identifier:
            raise ValueError(f"没找到元素：{selector}")
        return identifier

    async def _post_raw(self, path: str, payload: dict[str, Any]) -> object:
        """POST 到会话端点，返回原始 ``value``（可能不是对象）。"""
        assert self._client is not None
        try:
            response = await self._client.post(
                f"{self._base}/session/{self._session}/{path}", json=payload
            )
            if response.status_code >= 400:
                logger.debug("WebDriver %s 返回 %s", path, response.status_code)
                return None
            return _raw_value(response)
        except httpx.HTTPError, ValueError:
            return None

    async def _post(
        self, path: str, payload: dict[str, Any], *, soft: bool = False
    ) -> dict[str, Any] | None:
        """POST 到会话端点，返回对象形式的 ``value``。"""
        value = await self._post_raw(path, payload)
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        if soft:
            return None
        return {}

    async def _get(self, path: str, *, soft: bool = False) -> dict[str, Any] | None:
        """GET 会话端点。"""
        assert self._client is not None
        try:
            response = await self._client.get(
                f"{self._base}/session/{self._session}/{path}"
            )
            return _value(response)
        except httpx.HTTPError, ValueError:
            if soft:
                return None
            raise


def _raw_value(response: httpx.Response) -> object:
    """取 WebDriver 响应里的原始 ``value``（可能是对象、字符串、布尔…）。"""
    data: object = response.json()
    if not isinstance(data, dict):
        raise ValueError("WebDriver 返回了非对象响应")
    return cast(dict[str, object], data).get("value")


def _value(response: httpx.Response) -> dict[str, Any]:
    """取出 WebDriver 响应里的 ``value`` 对象。"""
    data: object = response.json()
    if not isinstance(data, dict):
        raise ValueError("WebDriver 返回了非对象响应")
    value = cast(dict[str, Any], data).get("value")
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def jsonable(value: object) -> str:
    """把 JS 返回值压成一行文本。"""
    if value is None:
        return "null"
    text = str(value).replace("\n", " ")
    return text[:200]


def _free_port() -> int:
    """找一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


CONFIG_MODEL = BrowserConfig
"""契约测试使用的运行时配置模型。"""

TOOL = browser_operate
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    try:
        configure(load_runtime_config(BrowserConfig))
    except BlueError:
        configure(BrowserConfig())
    return run_tool(TOOL, "browser_operate")


if __name__ == "__main__":
    raise SystemExit(main())
