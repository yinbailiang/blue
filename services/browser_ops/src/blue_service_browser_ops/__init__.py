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
    "jsonable",
    "key_value_of",
    "main",
    "wrap_script",
]

TEXT_LIMIT = 1500
"""回传给模型的正文字符上限。"""

DRIVER_BOOT_TIMEOUT = 25.0
"""geckodriver 启动等待上限。"""

VIEWPORT_FIT_ATTEMPTS = 6
"""视口校准的最大迭代次数（每次用 ``window/rect`` 补偿外框占用）。"""

W3C_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
"""W3C 元素引用在 JSON 里的键名。"""

KEY_VALUES: dict[str, tuple[str, str]] = {
    "enter": ("\ue007", "Enter"),
    "return": ("\ue007", "Enter"),
    "tab": ("\ue004", "Tab"),
    "escape": ("\ue00c", "Escape"),
    "esc": ("\ue00c", "Escape"),
    "backspace": ("\ue003", "Backspace"),
    "delete": ("\ue017", "Delete"),
    "del": ("\ue017", "Delete"),
    "insert": ("\ue016", "Insert"),
    "space": (" ", "Space"),
    "pageup": ("\ue00e", "PageUp"),
    "pagedown": ("\ue00f", "PageDown"),
    "home": ("\ue011", "Home"),
    "end": ("\ue010", "End"),
    "arrowleft": ("\ue012", "ArrowLeft"),
    "left": ("\ue012", "ArrowLeft"),
    "arrowup": ("\ue013", "ArrowUp"),
    "up": ("\ue013", "ArrowUp"),
    "arrowright": ("\ue014", "ArrowRight"),
    "right": ("\ue014", "ArrowRight"),
    "arrowdown": ("\ue015", "ArrowDown"),
    "down": ("\ue015", "ArrowDown"),
}
"""按键名 → ``(W3C key 值, 物理键名 code)``。

``code`` 也要发：不少前端用 ``event.code`` 判断按键，只发 ``value`` 时它们认不出。
"""

MODIFIER_VALUES: dict[str, tuple[str, str]] = {
    "control": ("\ue009", "ControlLeft"),
    "ctrl": ("\ue009", "ControlLeft"),
    "shift": ("\ue008", "ShiftLeft"),
    "alt": ("\ue00a", "AltLeft"),
    "meta": ("\ue03d", "MetaLeft"),
    "cmd": ("\ue03d", "MetaLeft"),
    "command": ("\ue03d", "MetaLeft"),
}
"""修饰键名 → ``(W3C key 值, 物理键名 code)``（用于 ``press`` 里的 ``Control+a``）。"""

SET_VALUE_SCRIPT = """
const element = arguments[0];
const text = arguments[1];
if (element.isContentEditable) {
  element.textContent = text;
} else {
  element.value = text;
}
element.dispatchEvent(new Event('input', {bubbles: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
return element.isContentEditable ? element.textContent : element.value;
"""
"""``type`` 的后备实现：直接写值并派发 input/change 事件。"""


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
    key: str = Field(
        default="",
        description="按键名（press 用），如 Enter、Escape、Tab；可写 Control+a 组合键",
    )
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
        # --width/--height 是窗口外框（含标题栏/工具栏），视口会比请求矮一截；
        # 这里按实测差值补偿，保证截图尺寸与请求一致。
        await self.fit_viewport(width, height)

    async def fit_viewport(self, width: int, height: int) -> tuple[int, int]:
        """把**视口**调成请求的尺寸，返回最终 ``(innerWidth, innerHeight)``。"""
        inner_width = inner_height = 0
        for _ in range(VIEWPORT_FIT_ATTEMPTS):
            value = await self._post_raw(
                "execute/sync",
                {
                    "script": (
                        "return [window.innerWidth, window.innerHeight,"
                        " window.outerWidth, window.outerHeight];"
                    ),
                    "args": [],
                },
            )
            if not isinstance(value, list):
                return (inner_width, inner_height)
            items = cast(list[object], value)
            if len(items) < 4:
                return (inner_width, inner_height)
            sizes: list[int] = []
            for item in items[:4]:
                if isinstance(item, bool) or not isinstance(item, int | float):
                    return (inner_width, inner_height)
                sizes.append(int(item))
            inner_width, inner_height, outer_width, outer_height = sizes
            if inner_width == width and inner_height == height:
                return (inner_width, inner_height)
            await self._post_raw(
                "window/rect",
                {
                    "width": outer_width + (width - inner_width),
                    "height": outer_height + (height - inner_height),
                },
            )
            await asyncio.sleep(0.15)
        logger.debug(
            "视口校准未收敛：请求 %sx%s，实际 %sx%s",
            width,
            height,
            inner_width,
            inner_height,
        )
        return (inner_width, inner_height)

    async def navigate(self, url: str) -> None:
        """打开地址。"""
        await self._post_strict("url", {"url": url}, f"打开 {url}")

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
            script = wrap_script(action.text)
            value = await self._post_strict(
                "execute/sync", {"script": script, "args": []}, "执行 js"
            )
            return f"js → {jsonable(value)}"
        if kind == "click":
            element_id, element_key = await self._element(action.selector)
            # 元素贴视口边缘时原生点击的落点会在视口外，点不到也报 ok；
            # 先居中再点，失败再退回 JS click（遮挡层/动画元素很常见）。
            await self._scroll_into_view(element_id, element_key)
            try:
                await self._post_strict(
                    f"element/{element_id}/click",
                    {},
                    f"点击 {action.selector}",
                )
            except ValueError as error:
                logger.debug("原生点击失败，改用 JS click：%s", error)
                await self._post_strict(
                    "execute/sync",
                    {
                        "script": (
                            "arguments[0].scrollIntoView({block: 'center'});"
                            " arguments[0].click();"
                        ),
                        "args": [{element_key: element_id}],
                    },
                    f"点击 {action.selector}",
                )
                return f"click {action.selector} → ok（JS 回退）"
            return f"click {action.selector} → ok"
        if kind == "type":
            return await self._type(action)
        if kind == "press":
            return await self._press(action)
        if kind == "scroll":
            dy = int(float(action.value or "600"))
            value = await self._post_strict(
                "execute/sync",
                {
                    "script": (
                        f"window.scrollBy(0, {dy}); return Math.round(window.scrollY);"
                    ),
                    "args": [],
                },
                f"滚动 {dy}px",
            )
            return f"scroll {dy}px → y={jsonable(value)}"
        raise ValueError(f"不支持的动作：{kind}")

    async def _type(self, action: BrowserAction) -> str:
        """往元素里输入文本；``sendKeys`` 不受支持时退回 JS 写值。"""
        element_id, key_name = await self._element(action.selector)
        try:
            await self._post_strict(
                f"element/{element_id}/value",
                {"text": action.text},
                f"输入 {action.selector}",
            )
        except ValueError as error:
            logger.debug("sendKeys 失败，改用 JS 写值：%s", error)
            await self._post_strict(
                "execute/sync",
                {
                    "script": SET_VALUE_SCRIPT,
                    "args": [{key_name: element_id}, action.text],
                },
                f"输入 {action.selector}",
            )
        return f"type {action.selector} ({len(action.text)} 字符) → ok"

    async def _press(self, action: BrowserAction) -> str:
        """发送**真实**按键事件（W3C Perform Actions）。

        老实现用 ``elementSendKeys`` 把键名当文本发，页面收到的只是字符
        "Enter"，回车并不会触发提交；这里改用 actions 端点发 keydown/keyup。
        """
        combo = [
            part.strip() for part in (action.key or "Enter").split("+") if part.strip()
        ]
        if not combo:
            raise ValueError("press 需要 key")
        key_name, modifiers = combo[-1], combo[:-1]
        target = f" @{action.selector}" if action.selector.strip() else ""
        if action.selector.strip():
            element_id, element_key = await self._element(action.selector)
            await self._post_strict(
                "execute/sync",
                {
                    "script": "arguments[0].focus();",
                    "args": [{element_key: element_id}],
                },
                f"聚焦 {action.selector}",
            )
        events: list[dict[str, str]] = []
        held: list[tuple[str, str]] = []
        for modifier in modifiers:
            pair = MODIFIER_VALUES.get(modifier.lower())
            if pair is None:
                raise ValueError(f"不认识的修饰键：{modifier}")
            events.append({"type": "keyDown", "value": pair[0], "code": pair[1]})
            held.append(pair)
        key_value, key_code = key_value_of(key_name)
        events.append({"type": "keyDown", "value": key_value, "code": key_code})
        events.append({"type": "keyUp", "value": key_value, "code": key_code})
        for modifier_value, modifier_code in reversed(held):
            events.append(
                {"type": "keyUp", "value": modifier_value, "code": modifier_code}
            )
        await self._post_strict(
            "actions",
            {"actions": [{"type": "key", "id": "keyboard", "actions": events}]},
            f"按键 {'+'.join(combo)}",
        )
        return f"press {'+'.join(combo)}{target} → ok"

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

    async def _element(self, selector: str) -> tuple[str, str]:
        """按 CSS 选择器取元素引用，返回 ``(元素 id, JSON 里的键名)``。"""
        if not selector.strip():
            raise ValueError("该动作需要 selector")
        try:
            value = await self._post_strict(
                "element",
                {"using": "css selector", "value": selector},
                f"查找元素 {selector}",
            )
        except ValueError as error:
            raise ValueError(f"没找到元素：{selector}（{error}）") from None
        if not isinstance(value, dict):
            raise ValueError(f"没找到元素：{selector}")
        for key, identifier in cast(dict[str, Any], value).items():
            if isinstance(identifier, str) and identifier:
                return identifier, key
        raise ValueError(f"没找到元素：{selector}")

    async def _scroll_into_view(self, element_id: str, element_key: str) -> None:
        """把元素滚到视口中央；失败不致命（原生点击仍会尝试）。"""
        with contextlib.suppress(ValueError):
            await self._post_strict(
                "execute/sync",
                {
                    "script": (
                        "arguments[0].scrollIntoView({block: 'center',"
                        " inline: 'center'});"
                    ),
                    "args": [{element_key: element_id}],
                },
                "滚动到元素",
            )

    async def _post_strict(
        self, path: str, payload: dict[str, Any], what: str
    ) -> object:
        """POST 到会话端点并返回原始 ``value``；WebDriver 报错时抛出带原因的错误。

        以前的实现把这些错误吞掉了，于是「点击没生效」「输入没进去」在结果里
        看起来仍然是成功的。
        """
        assert self._client is not None
        try:
            response = await self._client.post(
                f"{self._base}/session/{self._session}/{path}", json=payload
            )
        except httpx.HTTPError as error:
            raise ValueError(f"{what} 失败：{error}") from error
        if response.status_code >= 400:
            raise ValueError(f"{what} 失败：{_error_message(response)}")
        return _raw_value(response)

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


def wrap_script(script: str) -> str:
    """让「裸表达式」也能把值带回来：没有 ``return`` 时自动包一层。

    多语句脚本（中间有分号）原样执行，避免包成语法错误。
    """
    body = script.strip()
    if not body:
        raise ValueError("js 动作需要 text")
    if "return" in body:
        return body
    expression = body.rstrip(";").strip()
    if ";" in expression:
        return body
    return f"return ({expression});"


def key_value_of(name: str) -> tuple[str, str]:
    """按键名（或单个字符）→ ``(W3C key 值, 物理键名 code)``。"""
    lowered = name.lower()
    if lowered in KEY_VALUES:
        return KEY_VALUES[lowered]
    if len(name) == 1:
        if name.isdigit():
            return (name, f"Digit{name}")
        if name.isascii() and name.isalpha():
            return (name, f"Key{name.upper()}")
        return (name, "")
    raise ValueError(f"不支持的按键：{name}")


def _error_message(response: httpx.Response) -> str:
    """从 WebDriver 的错误响应里取出可读原因。"""
    try:
        data: object = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(data, dict):
        return f"HTTP {response.status_code}"
    value = cast(dict[str, object], data).get("value")
    if isinstance(value, dict):
        detail = cast(dict[str, object], value)
        error = str(detail.get("error") or "")
        lines = str(detail.get("message") or "").strip().splitlines()
        message = lines[0] if lines else ""
        if error and message:
            return f"{error}: {message}"
        if error or message:
            return error or message
    return f"HTTP {response.status_code}"


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
