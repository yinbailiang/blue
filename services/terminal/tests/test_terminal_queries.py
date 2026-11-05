"""终端查询应答：PTY 必须回答 DSR / DA / OSC，否则 TUI 会等到超时（nvim 的 E1568）。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from blue_service_terminal.config import TerminalConfig
from blue_service_terminal.session import SessionManager, TerminalSession

NVIM = shutil.which("nvim")

_ASKER = (
    "import os, select, tty\n"
    "tty.setraw(0)\n"
    "os.write(1, b'\\x1b[5n')\n"  # DSR 5：状态就绪
    "os.write(1, b'\\x1b[6n')\n"  # CPR：光标位置
    "os.write(1, b'\\x1b[c')\n"  # DA1：设备属性
    "os.write(1, b'\\x1b]11;?\\x07')\n"  # OSC 11：背景色
    "got = b''\n"
    "ready, _, _ = select.select([0], [], [], 2.0)\n"
    "while ready:\n"
    "    got += os.read(0, 4096)\n"
    "    ready, _, _ = select.select([0], [], [], 0.2)\n"
    "open({out!r}, 'wb').write(got)\n"
)
"""一个把收到的应答存盘的探针程序（raw 模式，免得等换行）。"""


def _config() -> TerminalConfig:
    return TerminalConfig()


def _open(
    manager: SessionManager, *, respond_to_queries: bool = True
) -> TerminalSession:
    config = _config()
    return manager.open(
        shell=config.resolve_shell(),
        cwd=Path("/tmp"),
        cols=100,
        rows=30,
        scrollback=300,
        prompt=config.prompt,
        isolate_rc=config.isolate_rc,
        background=config.background,
        foreground=config.foreground,
        respond_to_queries=respond_to_queries,
    )


async def _ask(tmp_path: Path, *, respond: bool) -> bytes:
    """跑一次探针，返回它收到的字节。"""
    result = tmp_path / ("answered.bin" if respond else "silent.bin")
    script = tmp_path / "ask.py"
    script.write_text(_ASKER.format(out=str(result)))

    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = _open(manager, respond_to_queries=respond)
        await asyncio.sleep(1.0)
        session.write(f"python3 {script}")
        await asyncio.sleep(2.0)
        return result.read_bytes() if result.is_file() else b""
    finally:
        await manager.stop()


def test_pty_answers_all_queries(tmp_path: Path) -> None:
    """四类查询都要有应答：DSR 5、CPR、DA1、OSC 11。"""
    raw = asyncio.run(_ask(tmp_path, respond=True))

    assert b"\x1b[0n" in raw, "DSR 5 未应答"
    assert b"\x1b[?1;2c" in raw, "DA1 未应答"
    assert b"\x1b]11;rgb:" in raw, "OSC 11 未应答"
    # CPR 的形状是 ESC [ 行 ; 列 R
    assert b"R" in raw and raw.index(b"R") > raw.index(b"\x1b[0n"), "CPR 未应答或顺序错"


def test_osc_answer_carries_configured_color(tmp_path: Path) -> None:
    """OSC 11 应答里的颜色必须来自配置，而不是写死的值。"""
    result = tmp_path / "asked.bin"
    script = tmp_path / "ask_color.py"
    script.write_text(_ASKER.format(out=str(result)))

    async def run() -> bytes:
        manager = SessionManager(max_sessions=2, idle_ttl=60.0)
        await manager.start()
        try:
            session = manager.open(
                shell=_config().resolve_shell(),
                cwd=Path("/tmp"),
                cols=100,
                rows=30,
                scrollback=300,
                prompt=_config().prompt,
                isolate_rc=_config().isolate_rc,
                background="#123456",
                foreground="#abcdef",
            )
            await asyncio.sleep(1.0)
            session.write(f"python3 {script}")
            await asyncio.sleep(2.0)
            return result.read_bytes() if result.is_file() else b""
        finally:
            await manager.stop()

    raw = asyncio.run(run())

    assert b"\x1b]11;rgb:1212/3434/5656" in raw


def test_queries_can_be_switched_off(tmp_path: Path) -> None:
    """关掉应答后就不该有回话——这个开关得真的起作用。"""
    raw = asyncio.run(_ask(tmp_path, respond=False))

    assert raw == b""


@pytest.mark.skipif(NVIM is None, reason="没有安装 nvim")
def test_nvim_no_longer_reports_e1568() -> None:
    """端到端：nvim 启动时发 OSC 11 / DSR 5，应答到位就不报 E1568。"""
    asyncio.run(_nvim_startup())


async def _nvim_startup() -> None:
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = _open(manager, respond_to_queries=True)
        await asyncio.sleep(1.0)
        session.write("nvim -u NONE /tmp/e1568_probe.txt")
        await asyncio.sleep(3.0)
        text = session.text(30)
        assert "E1568" not in text
    finally:
        await manager.stop()
