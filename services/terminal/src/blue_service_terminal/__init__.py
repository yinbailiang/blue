"""blue 外置工具：多会话交互式终端（PTY），带截图与坐标点击。

与 ``shell`` 的分工：

- ``shell``：跑一条命令、收 stdout/stderr、结束。适合 `pytest`、`git status`。
- ``terminal``：**长期存活的 PTY**。适合 vim / htop / python REPL / ssh 这类
  全屏或交互式程序；``pyte`` 把 ANSI 流解析成屏幕网格，所以能「截图给模型看」，
  也能把截图上的一次点击换算成终端鼠标事件发给 TUI。

多工具同进程：``ToolWorker`` 只允许一进程一工具，这里仿 ``mcp_bridge`` 的做法，
用 ``ToolRegistrar`` 持有一组租约（``ToolLease``），``handle_call`` 按名字分派。
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import time
from pathlib import Path
from typing import Annotated, Any

from blue_contracts_tool import (
    ToolCallRequest,
    ToolCallResult,
    ToolResultEvent,
)
from infinity_agent import FunctionCall, ToolCall
from infinity_agent.tools import ToolRegistry
from infinity_bus import EventBus
from PIL import Image
from pydantic import BaseModel, Field

from blue.runtime import build_runtime, load_runtime_config, run_service
from blue.transport import BusRuntime
from blue_service_terminal.config import TerminalConfig
from blue_service_terminal.render import (
    FontSet,
    geometry_for,
    render_screen,
    resolve_fonts,
)
from blue_service_terminal.session import (
    SessionManager,
    TerminalSession,
    supports_line_editing,
)
from blue_tool_sdk import (
    ToolCallHandler,
    ToolLease,
    ToolRegistrar,
    unpack_tool_output,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_MODEL",
    "TerminalConfig",
    "TerminalFrame",
    "TerminalInfo",
    "TerminalService",
    "TerminalView",
    "main",
    "terminal_click",
    "terminal_close",
    "terminal_drag",
    "terminal_list",
    "terminal_open",
    "terminal_read",
    "terminal_resize",
    "terminal_snapshot",
    "terminal_write",
]

READ_LINES = 40
"""``terminal_read`` / 各操作回读的默认行数。"""

MAX_SNAPSHOT_EDGE = 1600
"""截图长边上限（模型侧不需要更大）。"""

_config: TerminalConfig = TerminalConfig()
"""进程级配置；由 :func:`configure` 注入。"""

_manager: SessionManager | None = None
"""进程级会话池；由 :meth:`TerminalService.start` 注入。"""

_fonts_cache: FontSet | None = None
"""解析后的字体组合（``None`` 表示还没算过）。"""


def configure(config: TerminalConfig) -> None:
    """设置本进程后续会话使用的配置。"""
    global _config, _fonts_cache
    _config = config
    _fonts_cache = None


def _fonts() -> FontSet:
    """当前字体组合（延迟解析，找不到就用内置字体）。"""
    global _fonts_cache
    if _fonts_cache is None:
        main_path, cjk_path = resolve_fonts(_config.font_path, _config.cjk_font_path)
        _fonts_cache = FontSet(
            main_path=main_path, cjk_path=cjk_path, size=_config.font_size
        )
    return _fonts_cache


def _require_manager() -> SessionManager:
    """取会话池（服务未启动时报错而不是崩）。"""
    if _manager is None:
        raise RuntimeError("终端服务尚未启动")
    return _manager


class TerminalInfo(BaseModel):
    """一个会话的元信息。"""

    session_id: str = Field(description="会话 ID（后续调用的句柄）")
    shell: str = Field(description="启动的命令行")
    cwd: str = Field(description="工作目录")
    cols: int = Field(description="列数")
    rows: int = Field(description="行数")
    alive: bool = Field(description="PTY 是否还在运行")
    exit_code: int | None = Field(default=None, description="已退出时的退出码")
    mouse_tracking: bool = Field(
        default=False, description="程序是否启用了鼠标跟踪（决定点击是否有效）"
    )
    idle_seconds: float = Field(default=0.0, description="空闲秒数")


class TerminalView(BaseModel):
    """一次操作后的屏幕快照（文本）。"""

    session_id: str = Field(description="会话 ID")
    cols: int = Field(description="列数")
    rows: int = Field(description="行数")
    alive: bool = Field(description="PTY 是否还在运行")
    cursor_col: int = Field(default=1, description="光标列（1 起）")
    cursor_row: int = Field(default=1, description="光标行（1 起）")
    mouse_tracking: bool = Field(default=False, description="程序是否启用鼠标跟踪")
    text: str = Field(default="", description="屏幕文本（右侧空白已裁掉）")
    note: str = Field(default="", description="附加说明")


class TerminalFrame(TerminalView):
    """一次操作后的终端截图（PNG 附件）。"""

    image_width: int = Field(default=0, description="截图宽度（像素）")
    image_height: int = Field(default=0, description="截图高度（像素）")
    attachments: list[dict[str, str]] = Field(
        default_factory=list[dict[str, str]], description="PNG 附件"
    )


def _info(session: TerminalSession) -> TerminalInfo:
    """会话 → 元信息。"""
    return TerminalInfo(
        session_id=session.session_id,
        shell=session.shell,
        cwd=str(session.cwd),
        cols=session.cols,
        rows=session.rows,
        alive=session.alive,
        exit_code=session.exit_code,
        mouse_tracking=session.mouse_tracking,
        idle_seconds=round(time.time() - session.last_used, 3),
    )


def _view(session: TerminalSession, *, lines: int = READ_LINES) -> TerminalView:
    """会话 → 文本视图。"""
    col, row = session.cursor
    note = ""
    if not session.alive:
        note = "会话进程已退出；内容仍可读，用完请 terminal_close"
    return TerminalView(
        session_id=session.session_id,
        cols=session.cols,
        rows=session.rows,
        alive=session.alive,
        cursor_col=col + 1,
        cursor_row=row + 1,
        mouse_tracking=session.mouse_tracking,
        text=session.text(lines),
        note=note,
    )


def terminal_open(
    shell: Annotated[
        str, "要启动的 shell 或交互命令；空=服务默认（如 bash、python3 -i）"
    ] = "",
    cwd: Annotated[str, "工作目录；空=服务默认（项目根）"] = "",
    cols: Annotated[int, Field(ge=20, le=400), "列数；0=服务默认"] = 0,
    rows: Annotated[int, Field(ge=5, le=200), "行数；0=服务默认"] = 0,
) -> TerminalView:
    """新开一个交互式终端会话（PTY），返回初始屏幕。"""
    manager = _require_manager()
    workdir = Path(cwd).expanduser() if cwd.strip() else _config.resolve_workdir()
    if not workdir.is_dir():
        raise ValueError(f"工作目录不存在：{workdir}")
    chosen = shell.strip() or _config.resolve_shell()
    session = manager.open(
        shell=chosen,
        cwd=workdir,
        cols=cols or _config.cols,
        rows=rows or _config.rows,
        scrollback=_config.scrollback,
        prompt=_config.prompt,
        isolate_rc=_config.isolate_rc,
    )
    logger.info(
        "已打开终端会话 %s（%s @ %s）", session.session_id, session.shell, workdir
    )
    view = _view(session)
    if supports_line_editing(chosen) is False:
        note = (
            f"注意：{chosen} 没有链接 readline（方向键/历史不可用，"
            "提示符里的 \\[ \\] 会原样打印）。换用 bash-interactive 或 zsh。"
        )
        view = view.model_copy(update={"note": note})
    return view


def terminal_write(
    session_id: Annotated[str, "会话 ID"],
    text: Annotated[str, "要输入的文本（支持转义序列，如 \\u0003 表示 Ctrl-C）"],
    newline: Annotated[bool, "是否自动补回车"] = True,
) -> TerminalView:
    """往终端输入一行文本（像人打字后按回车）。"""
    session = _require_manager().get(session_id)
    session.write(text, newline=newline)
    return _view(session)


def terminal_read(
    session_id: Annotated[str, "会话 ID"],
    lines: Annotated[
        int, Field(ge=1, le=2000), "读取最近多少行（含回滚历史）"
    ] = READ_LINES,
) -> TerminalView:
    """读取终端最近的输出（含回滚历史，右侧空白已裁掉）。"""
    session = _require_manager().get(session_id)
    return _view(session, lines=lines)


def terminal_snapshot(
    session_id: Annotated[str, "会话 ID"],
    max_edge: Annotated[
        int, Field(ge=200, le=4000), "图片长边上限"
    ] = MAX_SNAPSHOT_EDGE,
) -> TerminalFrame:
    """把终端当前画面渲染成 PNG 截图（作为图片附件返回）。"""
    session = _require_manager().get(session_id)
    fonts = _fonts()
    geometry = geometry_for(
        cols=session.cols,
        rows=session.rows,
        font_path=fonts.main_path,
        font_size=fonts.size,
    )
    png = render_screen(session.cells(), geometry, fonts=fonts)
    png, width, height = _shrink(png, max_edge)
    view = _view(session)
    return TerminalFrame(
        **view.model_dump(),
        image_width=width,
        image_height=height,
        attachments=[
            {
                "mime": "image/png",
                "data": base64.b64encode(png).decode("ascii"),
                "name": f"{session.session_id}.png",
            }
        ],
    )


def terminal_click(
    session_id: Annotated[str, "会话 ID"],
    x: Annotated[int, Field(ge=0), "截图像素 x（与 terminal_snapshot 同一坐标系）"] = 0,
    y: Annotated[int, Field(ge=0), "截图像素 y"] = 0,
    button: Annotated[str, "left / middle / right"] = "left",
    count: Annotated[int, Field(ge=1, le=3), "点击次数"] = 1,
    col: Annotated[int, Field(ge=0), "直接给列号（1 起）；>0 时忽略像素坐标"] = 0,
    row: Annotated[int, Field(ge=0), "直接给行号（1 起）"] = 0,
) -> TerminalView:
    """在终端画面上点击（按截图坐标或行列）。

    只有启用了鼠标跟踪的 TUI 程序会响应；普通 shell 里点击无副作用。
    """
    session = _require_manager().get(session_id)
    target_col, target_row = _point(session, x=x, y=y, col=col, row=row)
    session.click(target_col, target_row, button=button, count=count)
    view = _view(session)
    hint = "" if session.mouse_tracking else "（程序未启用鼠标跟踪，点击可能无效果）"
    return view.model_copy(update={"note": f"点击 ({target_col}, {target_row}){hint}"})


def terminal_drag(
    session_id: Annotated[str, "会话 ID"],
    from_x: Annotated[int, Field(ge=0), "起点像素 x"] = 0,
    from_y: Annotated[int, Field(ge=0), "起点像素 y"] = 0,
    to_x: Annotated[int, Field(ge=0), "终点像素 x"] = 0,
    to_y: Annotated[int, Field(ge=0), "终点像素 y"] = 0,
    button: Annotated[str, "left / middle / right"] = "left",
    steps: Annotated[int, Field(ge=1, le=50), "中间移动步数"] = 8,
    from_col: Annotated[int, Field(ge=0), "起点列（1 起）；>0 时忽略像素"] = 0,
    from_row: Annotated[int, Field(ge=0), "起点行"] = 0,
    to_col: Annotated[int, Field(ge=0), "终点列"] = 0,
    to_row: Annotated[int, Field(ge=0), "终点行"] = 0,
) -> TerminalView:
    """按住鼠标从一点拖到另一点（终端里的拖选 / 拖动）。"""
    session = _require_manager().get(session_id)
    start = _point(session, x=from_x, y=from_y, col=from_col, row=from_row)
    end = _point(session, x=to_x, y=to_y, col=to_col, row=to_row)
    session.drag(start, end, button=button, steps=steps)
    view = _view(session)
    hint = "" if session.mouse_tracking else "（程序未启用鼠标跟踪，拖动可能无效果）"
    return view.model_copy(
        update={
            "note": f"从 {start} 拖到 {end}{hint}",
        }
    )


def terminal_resize(
    session_id: Annotated[str, "会话 ID"],
    cols: Annotated[int, Field(ge=20, le=400), "新列数"],
    rows: Annotated[int, Field(ge=5, le=200), "新行数"],
) -> TerminalView:
    """调整终端大小（会通知子进程重排）。"""
    session = _require_manager().get(session_id)
    session.resize(cols, rows)
    return _view(session)


def terminal_list() -> list[TerminalInfo]:
    """列出所有终端会话。"""
    return [_info(session) for session in _require_manager().list()]


def terminal_close(
    session_id: Annotated[str, "会话 ID"],
) -> bool:
    """关闭一个终端会话（结束其进程组）；返回是否确实存在。"""
    return _require_manager().close(session_id)


def _point(
    session: TerminalSession, *, x: int, y: int, col: int, row: int
) -> tuple[int, int]:
    """把「像素坐标或行列」统一成行列（1 起）。"""
    if col > 0 or row > 0:
        return max(1, col or 1), max(1, row or 1)
    fonts = _fonts()
    geometry = geometry_for(
        cols=session.cols,
        rows=session.rows,
        font_path=fonts.main_path,
        font_size=fonts.size,
    )
    return geometry.to_cell(x, y)


def _shrink(png: bytes, max_edge: int) -> tuple[bytes, int, int]:
    """按长边等比缩小截图。"""
    with Image.open(io.BytesIO(png)) as raw:
        image = raw.convert("RGB")
        if max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), image.width, image.height
        return png, image.width, image.height


TOOLS: tuple[Any, ...] = (
    terminal_open,
    terminal_write,
    terminal_read,
    terminal_snapshot,
    terminal_click,
    terminal_drag,
    terminal_resize,
    terminal_list,
    terminal_close,
)
"""本进程对外提供的全部工具。"""


class TerminalService:
    """终端服务：一个进程承载全部终端工具 + 一个会话池。"""

    def __init__(self, *, runtime: BusRuntime, config: TerminalConfig) -> None:
        self._runtime = runtime
        self._config = config
        self._manager = SessionManager(
            max_sessions=config.max_sessions, idle_ttl=config.idle_ttl
        )
        self._registry = ToolRegistry()
        for tool in TOOLS:
            self._registry.register(tool)
        self._definitions = self._registry.definitions
        self._registrar: ToolRegistrar | None = None

    @property
    def definitions(self) -> list[Any]:
        """本服务提供的工具定义（调试与测试用）。"""
        return list(self._definitions)

    async def start(self) -> None:
        """启会话池、连总线、登记全部工具租约。"""
        global _manager
        configure(self._config)
        _manager = self._manager
        await self._manager.start()
        try:
            await self._runtime.start()
            await self._runtime.register(ToolCallHandler(self))
        except Exception:
            await self._manager.stop()
            await self._runtime.stop()
            raise
        self._registrar = ToolRegistrar(
            runtime=self._runtime,
            leases=[
                ToolLease(
                    worker_id=f"tool.terminal.{item.function.name}", definition=item
                )
                for item in self._definitions
            ],
            ttl=15.0,
        )
        await self._registrar.announce()
        logger.info(
            "终端服务已上线：%s 个工具（%s）",
            len(self._definitions),
            ", ".join(item.function.name for item in self._definitions),
        )

    async def stop(self) -> None:
        """注销工具、关闭全部会话、断开总线。"""
        global _manager
        registrar, self._registrar = self._registrar, None
        if registrar is not None:
            with contextlib.suppress(Exception):
                await registrar.withdraw()
        await self._manager.stop()
        await self._runtime.stop()
        if _manager is self._manager:
            _manager = None

    async def handle_call(
        self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """按工具名分派执行，并回 ``tool.result``（成功或失败都必达）。"""
        names = {item.function.name for item in self._definitions}
        if payload.tool not in names:
            return
        message = await self._registry.invoke(
            ToolCall(
                id=payload.request_id,
                function=FunctionCall(name=payload.tool, arguments=payload.arguments),
            )
        )
        text, ok, attachments = unpack_tool_output(message.content)
        if not ok:
            logger.warning("终端工具 %s 执行失败：%s", payload.tool, text)
        await bus_proxy.publish(
            ToolResultEvent.name,
            ToolCallResult(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=ok,
                error_msg=None if ok else text,
                content=text,
                attachments=attachments,
            ),
        )


CONFIG_MODEL = TerminalConfig
"""契约测试使用的运行时配置模型。"""


def main() -> int:
    """控制台脚本入口。"""
    config = load_runtime_config(TerminalConfig)
    runtime = build_runtime("terminal")
    return run_service(TerminalService(runtime=runtime, config=config))


if __name__ == "__main__":
    raise SystemExit(main())
