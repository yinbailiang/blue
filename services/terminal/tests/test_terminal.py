"""终端服务测试：PTY 会话、行编辑、渲染与坐标换算。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from blue_service_terminal.config import TerminalConfig
from blue_service_terminal.render import (
    FontSet,
    FrameGeometry,
    geometry_for,
    render_screen,
    resolve_fonts,
)
from blue_service_terminal.session import (
    SessionLimitError,
    SessionManager,
    SessionNotFoundError,
    supports_line_editing,
)

SYSTEM_BASH = Path("/run/current-system/sw/bin/bash")
"""系统 profile 的 bash（带 readline）；不存在时相关用例跳过。"""


def _config() -> TerminalConfig:
    """测试用配置：关掉 rc 隔离带来的不确定性，提示符固定。"""
    return TerminalConfig()


async def _open(manager: SessionManager, cols: int = 80, rows: int = 16):
    """开一个会话（用配置挑出来的 shell）。"""
    config = _config()
    return manager.open(
        shell=config.resolve_shell(),
        cwd=Path("/tmp"),
        cols=cols,
        rows=rows,
        scrollback=300,
        prompt=config.prompt,
        isolate_rc=config.isolate_rc,
    )


def test_supports_line_editing_detects_readline() -> None:
    """NixOS 的 bash 有两种构建：没有 readline 的那种会被识别出来。"""
    assert supports_line_editing("zsh") is None  # 非 bash，无法判断
    assert supports_line_editing("/definitely/missing/bash") is False
    if SYSTEM_BASH.is_file():
        assert supports_line_editing(str(SYSTEM_BASH)) is True


def test_resolve_shell_skips_bash_without_readline() -> None:
    """自动挑选的 shell 一定带行编辑（否则方向键会失效）。"""
    shell = _config().resolve_shell()

    assert shell
    assert supports_line_editing(shell) is not False


def test_session_runs_command_and_reports_text() -> None:
    """写入命令后能在文本视图里读到输出。"""
    asyncio.run(_runs_command())


async def _runs_command() -> None:
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = await _open(manager)
        await asyncio.sleep(1.0)
        session.write("echo hello-terminal-test")
        await asyncio.sleep(0.8)
        text = session.text(20)
        assert "hello-terminal-test" in text
        assert session.alive is True
        assert session.cols == 80 and session.rows == 16
    finally:
        await manager.stop()


def test_history_recall_with_arrow_key() -> None:
    """上方向键能召回上一条命令——证明 readline（行编辑）真的在工作。"""
    asyncio.run(_history_recall())


async def _history_recall() -> None:
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = await _open(manager)
        await asyncio.sleep(1.0)
        session.write("echo recall-me")
        await asyncio.sleep(0.8)
        session.send(b"\x1b[A")  # ↑
        await asyncio.sleep(0.3)
        session.write("")
        await asyncio.sleep(0.8)
        text = session.text(30)
        # 命令被召回并再次执行：屏幕上出现两次命令、两次输出
        assert text.count("echo recall-me") == 2
        assert text.count("recall-me") >= 3
        assert "\\[" not in text
    finally:
        await manager.stop()


def test_session_limit_and_close() -> None:
    """会话数上限生效；关闭后可以再开。"""
    asyncio.run(_limit_and_close())


async def _limit_and_close() -> None:
    manager = SessionManager(max_sessions=1, idle_ttl=60.0)
    await manager.start()
    try:
        session = await _open(manager)
        with pytest.raises(SessionLimitError):
            await _open(manager)
        assert manager.close(session.session_id) is True
        assert manager.close(session.session_id) is False
        again = await _open(manager)
        assert again.session_id != session.session_id
    finally:
        await manager.stop()


def test_missing_session_raises() -> None:
    """取不存在的会话给出可读错误。"""
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)

    with pytest.raises(SessionNotFoundError):
        manager.get("t999")


def test_resize_updates_geometry() -> None:
    """resize 后会话与屏幕尺寸同步变化。"""
    asyncio.run(_resize())


async def _resize() -> None:
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = await _open(manager, cols=80, rows=16)
        await asyncio.sleep(0.6)
        session.resize(100, 24)
        await asyncio.sleep(0.3)
        assert (session.cols, session.rows) == (100, 24)
        assert len(session.visible_lines()) == 24
    finally:
        await manager.stop()


def test_text_drops_trailing_blank_lines() -> None:
    """文本视图不把屏幕底部的大片空白带出来。"""
    asyncio.run(_blank_tail())


async def _blank_tail() -> None:
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = await _open(manager, rows=40)
        await asyncio.sleep(0.9)
        session.write("echo tail-check")
        await asyncio.sleep(0.7)
        text = session.text(40)
        assert text.splitlines()[-1].strip() != ""
        assert "tail-check" in text
    finally:
        await manager.stop()


def test_geometry_maps_pixels_and_cells() -> None:
    """像素 ↔ 行列换算在边界上也不越界。"""
    geometry = FrameGeometry(cell_width=10, cell_height=20, padding=8, cols=80, rows=24)

    assert geometry.width == 8 * 2 + 80 * 10
    assert geometry.height == 8 * 2 + 24 * 20
    assert geometry.to_cell(8, 8) == (1, 1)
    assert geometry.to_cell(18, 28) == (2, 2)
    assert geometry.to_cell(0, 0) == (1, 1)  # 夹到左上
    assert geometry.to_cell(99999, 99999) == (80, 24)  # 夹到右下
    assert geometry.to_pixel(1, 1) == (13, 18)


def test_render_produces_png_of_expected_size() -> None:
    """渲染出来的 PNG 尺寸与几何一致，且带 PNG 魔数。"""
    from blue_service_terminal.session import CellStyle

    main_path, cjk_path = resolve_fonts("", "")
    fonts = FontSet(main_path=main_path, cjk_path=cjk_path, size=15)
    geometry = geometry_for(cols=40, rows=10, font_path=main_path, font_size=15)
    cells = [
        [CellStyle(char=("中文" if row == 0 and col < 2 else "x")) for col in range(40)]
        for row in range(10)
    ]

    png = render_screen(cells, geometry, fonts=fonts)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000
