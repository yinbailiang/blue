"""browser 工具测试：参数校验、真实截图（本机 Firefox）与失败路径。"""

from __future__ import annotations

import asyncio
import base64
import shutil

import pytest
from blue_service_browser import BrowserConfig, capture, configure

FIREFOX = shutil.which("firefox") or "/run/current-system/sw/bin/firefox"
HAS_FIREFOX = shutil.which("firefox") is not None

PAGE = b"""<!doctype html><html><head><title>t</title></head>
<body style="background:#123;color:#eee;font:20px sans-serif">
<h1>browser tool</h1></body></html>"""


def test_capture_rejects_bad_url() -> None:
    """只接受 http/https 地址。"""
    configure(BrowserConfig())
    with pytest.raises(ValueError, match="http/https"):
        asyncio.run(capture("ftp://example.com/x"))
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(capture("   "))


@pytest.mark.skipif(not HAS_FIREFOX, reason="环境里没有 firefox")
def test_capture_returns_image_attachment() -> None:
    """对本地 HTTP 服务截图，返回图片附件。"""
    asyncio.run(_capture_local())


async def _capture_local() -> None:
    """起一个临时 HTTP 服务并截图。"""
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(BrowserConfig(firefox_bin=FIREFOX, timeout=45.0))
    try:
        shot = await capture(f"http://127.0.0.1:{port}/", 640, 480)
        assert shot.url.endswith(f":{port}/")
        assert (shot.width, shot.height) == (640, 480)
        assert shot.mime in ("image/png", "image/jpeg")
        assert len(shot.attachments) == 1
        blob = shot.attachments[0]
        data = base64.b64decode(blob["data"])
        assert len(data) == shot.size
        assert data[:2] in (b"\xff\xd8", b"\x89P")
    finally:
        server.close()
        await server.wait_closed()


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """最小 HTTP 处理器：任何路径都返回同一张页面。"""
    await reader.read(1024)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        + f"Content-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n".encode()
        + PAGE
    )
    await writer.drain()
    writer.close()


@pytest.mark.skipif(not HAS_FIREFOX, reason="环境里没有 firefox")
def test_capture_reports_unreachable_url() -> None:
    """连不上时给出明确错误，而不是抛底层异常。"""
    configure(BrowserConfig(firefox_bin=FIREFOX, timeout=8.0))
    with pytest.raises(ValueError, match=r"Firefox 截图失败|超时"):
        asyncio.run(capture("http://127.0.0.1:9/", 320, 240))
