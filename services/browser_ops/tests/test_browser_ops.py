"""browser_ops 测试：动作模型、端口分配与真实 WebDriver 交互。"""

from __future__ import annotations

import asyncio
import base64
import shutil

import pytest
from blue_service_browser_ops import (
    BrowserAction,
    BrowserConfig,
    browser_operate,
    configure,
    jsonable,
    key_value_of,
    wrap_script,
)
from pydantic import ValidationError

HAS_DRIVER = shutil.which("geckodriver") is not None
HAS_FIREFOX = shutil.which("firefox") is not None

PAGE = b"""<!doctype html><html><head><title>ops test</title></head>
<body style="font:16px sans-serif">
<h1 id="title">before</h1>
<button id="go"
  onclick="document.getElementById('title').textContent='after'">go</button>
<button id="covered"
  onclick="document.getElementById('title').textContent='covered'">covered</button>
<div id="overlay"
  style="position:fixed;top:0;left:0;width:100%;height:100%;
         background:rgba(0,0,0,0.01)"></div>
<form id="form"
  onsubmit="event.preventDefault();
            document.getElementById('title').textContent='submitted'">
  <input id="box" />
</form>
</body></html>"""


def test_action_type_is_enum() -> None:
    """动作类型只接受受支持的几种。"""
    assert BrowserAction(type="click", selector="#a").selector == "#a"
    with pytest.raises(ValidationError):
        BrowserAction(type="explode")  # pyright: ignore[reportArgumentType]


def test_action_rejects_unknown_fields() -> None:
    """拼错字段名直接报错，而不是被忽略。"""
    with pytest.raises(ValidationError):
        BrowserAction.model_validate({"type": "wait", "ms": 1, "selctor": "#a"})


def test_jsonable_truncates_and_flattens() -> None:
    """JS 返回值压成一行并截断。"""
    assert jsonable(None) == "null"
    assert jsonable("a\nb") == "a b"
    assert len(jsonable("x" * 500)) == 200


def testwrap_script_returns_bare_expression() -> None:
    """没有 return 的单表达式脚本自动包一层，值才能回到 steps。"""
    assert wrap_script("document.title") == "return (document.title);"
    assert wrap_script("document.title;") == "return (document.title);"
    assert wrap_script("return document.title") == "return document.title"


def testwrap_script_keeps_multi_statement() -> None:
    """多语句脚本原样执行，包装会变成语法错误。"""
    script = "const a = 1; const b = 2;"
    assert wrap_script(script) == script
    with pytest.raises(ValueError, match="需要 text"):
        wrap_script("   ")


def test_key_value_of_maps_names_and_chars() -> None:
    """按键名映射到 W3C (key, code)，单字符按需补 code，未知键报错。"""
    assert key_value_of("Enter") == ("\ue007", "Enter")
    assert key_value_of("escape") == ("\ue00c", "Escape")
    assert key_value_of("a") == ("a", "KeyA")
    assert key_value_of("7") == ("7", "Digit7")
    with pytest.raises(ValueError, match="不支持的按键"):
        key_value_of("Hyper")


def test_rejects_non_http_url() -> None:
    """只接受 http/https。"""
    configure(BrowserConfig())
    with pytest.raises(ValueError, match="http/https"):
        asyncio.run(browser_operate("file:///etc/passwd"))


@pytest.mark.skipif(
    not (HAS_DRIVER and HAS_FIREFOX), reason="需要 geckodriver 与 firefox"
)
def test_drives_page_and_returns_screenshot() -> None:
    """真实浏览器：等渲染 → 点击 → 输入 → 截图。"""
    asyncio.run(_drive_page())


async def _drive_page() -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(BrowserConfig(timeout=90.0))
    try:
        outcome = await browser_operate(
            f"http://127.0.0.1:{port}/",
            actions=[
                BrowserAction(type="js", text="return document.title"),
                # 裸表达式（没有 return）也应该把值带回来
                BrowserAction(type="js", text="document.title"),
                BrowserAction(type="click", selector="#go"),
                BrowserAction(type="wait", ms=100),
                BrowserAction(
                    type="js",
                    text="return document.getElementById('title').textContent",
                ),
                BrowserAction(type="type", selector="#box", text="hello"),
                BrowserAction(
                    type="js", text="return document.getElementById('box').value"
                ),
                # 真实按键：input 里回车应触发表单 submit
                BrowserAction(type="press", selector="#box", key="Enter"),
                BrowserAction(type="wait", ms=200),
                BrowserAction(
                    type="js",
                    text="return document.getElementById('title').textContent",
                ),
            ],
            wait_js="document.getElementById('go') !== null",
            wait_timeout=10.0,
            width=800,
            height=600,
        )
        joined = " | ".join(outcome.steps)
        assert "ops test" in joined
        assert "after" in joined  # 点击生效
        assert "hello" in joined  # 输入生效
        assert "submitted" in joined  # press Enter 真的按了回车
        assert "press Enter @#box → ok" in joined
        # 截图尺寸应与请求的视口一致（外框占位已被补偿）
        assert (outcome.width, outcome.height) == (800, 600)
        assert outcome.mime in ("image/png", "image/jpeg")
        assert len(outcome.attachments) == 1
        data = base64.b64decode(outcome.attachments[0]["data"])
        assert len(data) == outcome.size
        assert data[:2] in (b"\xff\xd8", b"\x89P")
        assert "submitted" in outcome.text or "after" in outcome.text
    finally:
        server.close()
        await server.wait_closed()


LINK_PAGE = b"""<!doctype html><html><head><title>link test</title></head>
<body style="margin:0;font:16px sans-serif">
<div style="height:1400px">spacer</div>
<a id="far" href="#jumped">jump</a>
</body></html>"""
"""远处（视口外）的链接：验证点击前会先滚到视口中央。"""


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """最小 HTTP 服务：``/link`` 返回带远端链接的页面。"""
    request = await reader.read(1024)
    parts = request.split(b" ", maxsplit=2)
    path = parts[1].decode() if len(parts) > 1 else "/"
    page = LINK_PAGE if path.startswith("/link") else PAGE
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        + f"Content-Length: {len(page)}\r\nConnection: close\r\n\r\n".encode()
        + page
    )
    await writer.drain()
    writer.close()


@pytest.mark.skipif(
    not (HAS_DRIVER and HAS_FIREFOX), reason="需要 geckodriver 与 firefox"
)
def test_click_falls_back_to_js_when_covered() -> None:
    """被遮挡层盖住的元素：原生点击失败时回退 JS click，而不是静默失败。"""
    asyncio.run(_click_covered())


async def _click_covered() -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(BrowserConfig(timeout=90.0))
    try:
        outcome = await browser_operate(
            f"http://127.0.0.1:{port}/",
            actions=[
                BrowserAction(type="click", selector="#covered"),
                BrowserAction(type="wait", ms=100),
                BrowserAction(
                    type="js",
                    text="return document.getElementById('title').textContent",
                ),
            ],
            width=600,
            height=400,
        )
        joined = " | ".join(outcome.steps)
        assert "click #covered → ok" in joined
        assert "covered" in joined.rsplit("js →", maxsplit=1)[-1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(
    not (HAS_DRIVER and HAS_FIREFOX), reason="需要 geckodriver 与 firefox"
)
def test_click_scrolls_far_element_into_view() -> None:
    """视口外的元素要先滚进来再点，否则原生点击落在视口外等于没点。"""
    asyncio.run(_click_far_element())


async def _click_far_element() -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(BrowserConfig(timeout=90.0))
    try:
        outcome = await browser_operate(
            f"http://127.0.0.1:{port}/link",
            actions=[
                BrowserAction(type="click", selector="#far"),
                BrowserAction(type="wait", ms=200),
                BrowserAction(type="js", text="return location.hash"),
            ],
            width=600,
            height=400,
        )
        joined = " | ".join(outcome.steps)
        assert "click #far → ok" in joined
        assert "#jumped" in joined.rsplit("js →", maxsplit=1)[-1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(
    not (HAS_DRIVER and HAS_FIREFOX), reason="需要 geckodriver 与 firefox"
)
def test_missing_element_reports_clear_error() -> None:
    """元素不存在时给出可读错误。"""
    asyncio.run(_missing_element())


async def _missing_element() -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(BrowserConfig(timeout=60.0))
    try:
        with pytest.raises(ValueError, match="没找到元素"):
            await browser_operate(
                f"http://127.0.0.1:{port}/",
                actions=[BrowserAction(type="click", selector="#nope")],
                width=400,
                height=300,
            )
    finally:
        server.close()
        await server.wait_closed()
