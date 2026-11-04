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
)
from pydantic import ValidationError

HAS_DRIVER = shutil.which("geckodriver") is not None
HAS_FIREFOX = shutil.which("firefox") is not None

PAGE = b"""<!doctype html><html><head><title>ops test</title></head>
<body style="font:16px sans-serif">
<h1 id="title">before</h1>
<button id="go"
  onclick="document.getElementById('title').textContent='after'">go</button>
<input id="box" />
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
        assert outcome.mime in ("image/png", "image/jpeg")
        assert len(outcome.attachments) == 1
        data = base64.b64decode(outcome.attachments[0]["data"])
        assert len(data) == outcome.size
        assert data[:2] in (b"\xff\xd8", b"\x89P")
        assert "before" in outcome.text or "after" in outcome.text
    finally:
        server.close()
        await server.wait_closed()


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """最小 HTTP 服务。"""
    await reader.read(1024)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        + f"Content-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n".encode()
        + PAGE
    )
    await writer.drain()
    writer.close()


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
