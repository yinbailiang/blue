"""blue 的 MCP 服务端样例：一块内存里的「虚拟屏幕」。

这是一个**独立进程的 MCP server**（stdio + newline-delimited JSON-RPC 2.0），
用来验证 `services/mcp_bridge` 的客户端链路：

```text
agent ──tool.call──> mcp_bridge ──tools/call──> 本进程（PIL 画布）
      <─tool.result──           <──content──┘
```

它刻意保持零额外依赖（只用标准库 + Pillow），也刻意让 ``screen_snapshot``
返回 **image content**：这样一条链路就能同时验证「工具注册」「工具调用」
与「图片附件回传」。

启动：``.venv/bin/python mcp_servers/virtual_screen/server.py``
"""

from __future__ import annotations

import base64
import io
import json
import sys
from typing import Any, cast

from PIL import Image, ImageColor, ImageDraw, ImageFont

PROTOCOL_VERSION = "2025-06-18"
"""与 blue 的 MCP 桥客户端声明的协议版本保持一致。"""

SERVER_INFO: dict[str, str] = {"name": "virtual-screen", "version": "0.1.0"}

WIDTH = 480
HEIGHT = 270
BACKGROUND = "#f4f6fb"
FOREGROUND = "#1f2937"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "screen_info",
        "description": "查看虚拟屏幕的尺寸、底色与已执行的操作数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screen_clear",
        "description": "把虚拟屏幕恢复为默认底色，清空所有绘制。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screen_fill",
        "description": "用纯色填充整块虚拟屏幕。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "color": {
                    "type": "string",
                    "description": "十六进制颜色，如 #1f2937；也接受英文色名",
                    "default": "#ffffff",
                }
            },
        },
    },
    {
        "name": "screen_rect",
        "description": "在虚拟屏幕上画一个矩形（可只画边框）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "左上角 x", "default": 0},
                "y": {"type": "integer", "description": "左上角 y", "default": 0},
                "width": {
                    "type": "integer",
                    "description": "宽（像素）",
                    "default": 120,
                },
                "height": {
                    "type": "integer",
                    "description": "高（像素）",
                    "default": 80,
                },
                "outline": {
                    "type": "string",
                    "description": "边框颜色；留空表示不画边框",
                    "default": "#3b82f6",
                },
                "fill": {
                    "type": "string",
                    "description": "填充颜色；留空表示透明",
                    "default": "",
                },
                "line_width": {
                    "type": "integer",
                    "description": "边框线宽",
                    "default": 3,
                },
            },
        },
    },
    {
        "name": "screen_text",
        "description": "在虚拟屏幕上写一行文字。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要写的文字"},
                "x": {"type": "integer", "description": "起始 x", "default": 16},
                "y": {"type": "integer", "description": "起始 y", "default": 16},
                "color": {
                    "type": "string",
                    "description": "文字颜色",
                    "default": FOREGROUND,
                },
                "size": {
                    "type": "integer",
                    "description": "字号（像素）",
                    "default": 24,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "screen_snapshot",
        "description": "把当前虚拟屏幕导出成 PNG 图片（MCP image content）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "追加到图片说明里的备注",
                    "default": "",
                }
            },
        },
    },
]
"""对外声明的工具集；``tools/list`` 原样返回。"""


class Screen:
    """一块可绘制、可导出的内存画布。"""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        self._width = width
        self._height = height
        self._image = Image.new("RGB", (width, height), BACKGROUND)
        self._draw = ImageDraw.Draw(self._image)
        self._ops = 0

    @property
    def width(self) -> int:
        """画布宽度。"""
        return self._width

    @property
    def height(self) -> int:
        """画布高度。"""
        return self._height

    @property
    def ops(self) -> int:
        """已执行的绘制操作数。"""
        return self._ops

    def clear(self) -> None:
        """恢复默认底色。"""
        self._image = Image.new("RGB", (self._width, self._height), BACKGROUND)
        self._draw = ImageDraw.Draw(self._image)
        self._ops = 0

    def fill(self, color: str) -> str:
        """整屏填充，返回实际使用的颜色。"""
        resolved = normalize_color(color)
        self._image = Image.new("RGB", (self._width, self._height), resolved)
        self._draw = ImageDraw.Draw(self._image)
        self._ops += 1
        return resolved

    def rect(
        self,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        outline: str,
        fill: str,
        line_width: int,
    ) -> int:
        """画矩形，返回覆盖的像素面积。"""
        box = (x, y, x + max(width, 1) - 1, y + max(height, 1) - 1)
        self._draw.rectangle(
            box,
            outline=normalize_color(outline) if outline else None,
            fill=normalize_color(fill) if fill else None,
            width=max(line_width, 1),
        )
        self._ops += 1
        return max(width, 1) * max(height, 1)

    def text(self, *, text: str, x: int, y: int, color: str, size: int) -> str:
        """写一行文字，返回写入的内容。"""
        font = ImageFont.load_default(size=max(size, 8))
        self._draw.text((x, y), text, fill=normalize_color(color), font=font)
        self._ops += 1
        return text

    def png(self) -> bytes:
        """导出 PNG 字节。"""
        buffer = io.BytesIO()
        self._image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def summary(self) -> dict[str, Any]:
        """尺寸与操作计数（放在文本 content 里）。"""
        return {
            "width": self._width,
            "height": self._height,
            "ops": self._ops,
            "background": BACKGROUND,
        }


def normalize_color(value: str) -> str:
    """校验颜色并规范成 ``#rrggbb``。

    :raises ValueError: 颜色无法解析
    """
    text = value.strip()
    if not text:
        raise ValueError("颜色不能为空")
    try:
        channels = ImageColor.getrgb(text)
    except ValueError as error:
        raise ValueError(f"无法识别的颜色：{value}") from error
    red, green, blue = channels[0], channels[1], channels[2]
    return f"#{red:02x}{green:02x}{blue:02x}"


def call_tool(name: str, arguments: dict[str, Any], screen: Screen) -> dict[str, Any]:
    """执行一个工具，返回 MCP ``tools/call`` 的 result。

    工具自身的失败走 ``isError`` 而不是 JSON-RPC error：这是 MCP 的约定，
    桥会把它映射成 ``tool.result(success=false)``。
    """
    try:
        return _dispatch(name, arguments, screen)
    except ValueError as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}


def _dispatch(name: str, arguments: dict[str, Any], screen: Screen) -> dict[str, Any]:
    """按工具名分派（参数不合法时抛 ``ValueError``）。"""
    if name == "screen_info":
        return _text_result(json.dumps(screen.summary(), ensure_ascii=False))
    if name == "screen_clear":
        screen.clear()
        return _text_result("虚拟屏幕已恢复默认底色。")
    if name == "screen_fill":
        color = screen.fill(_str_arg(arguments, "color", "#ffffff"))
        return _text_result(f"已整屏填充 {color}。")
    if name == "screen_rect":
        area = screen.rect(
            x=_int_arg(arguments, "x", 0),
            y=_int_arg(arguments, "y", 0),
            width=_int_arg(arguments, "width", 120),
            height=_int_arg(arguments, "height", 80),
            outline=_str_arg(arguments, "outline", "#3b82f6"),
            fill=_str_arg(arguments, "fill", ""),
            line_width=_int_arg(arguments, "line_width", 3),
        )
        return _text_result(f"已画矩形，覆盖 {area} 像素。")
    if name == "screen_text":
        text = _str_arg(arguments, "text", "").strip()
        if not text:
            raise ValueError("text 不能为空")
        written = screen.text(
            text=text,
            x=_int_arg(arguments, "x", 16),
            y=_int_arg(arguments, "y", 16),
            color=_str_arg(arguments, "color", FOREGROUND),
            size=_int_arg(arguments, "size", 24),
        )
        return _text_result(f"已写入文字：{written}")
    if name == "screen_snapshot":
        payload: dict[str, Any] = {
            "width": screen.width,
            "height": screen.height,
            "ops": screen.ops,
        }
        caption = f"虚拟屏幕快照 {screen.width}x{screen.height} · {screen.ops} 次操作"
        note = _str_arg(arguments, "note", "").strip()
        if note:
            caption = f"{caption} · {note}"
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"{caption}\n{json.dumps(payload, ensure_ascii=False)}",
                },
                {
                    "type": "image",
                    "data": base64.b64encode(screen.png()).decode("ascii"),
                    "mimeType": "image/png",
                },
            ],
            "isError": False,
            "structuredContent": payload,
        }
    raise ValueError(f"未知工具：{name}")


def _text_result(text: str) -> dict[str, Any]:
    """纯文本结果。"""
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _str_arg(arguments: dict[str, Any], key: str, default: str) -> str:
    """取字符串参数。"""
    value = arguments.get(key, default)
    return value if isinstance(value, str) else default


def _int_arg(arguments: dict[str, Any], key: str, default: int) -> int:
    """取整数参数（也接受字符串形式的整数）。"""
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def handle_message(message: dict[str, Any], screen: Screen) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；通知与客户端响应返回 ``None``（不写回）。"""
    method = message.get("method")
    if not isinstance(method, str):
        return None
    request_id = message.get("id")
    if request_id is None:
        return None
    params = message.get("params")
    arguments: dict[str, Any] = (
        cast(dict[str, Any], params) if isinstance(params, dict) else {}
    )
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = arguments.get("name")
        if not isinstance(name, str) or not name:
            return _error(request_id, -32602, "tools/call 缺少 name")
        raw = arguments.get("arguments")
        call_args: dict[str, Any] = (
            cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        )
        return _ok(request_id, call_tool(name, call_args, screen))
    if method == "ping":
        return _ok(request_id, {})
    return _error(request_id, -32601, f"method not found: {method}")


def _ok(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    """JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    """JSON-RPC 错误响应。"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> int:
    """从 stdin 逐行读 JSON-RPC，向 stdout 逐行写响应。"""
    screen = Screen()
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle_message(cast(dict[str, Any], message), screen)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
