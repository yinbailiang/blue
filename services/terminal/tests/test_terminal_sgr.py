"""冒号式 truecolor SGR 的改写：pyte 只认分号式，不认就会把参数当正文画上屏。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyte
from blue_service_terminal.config import TerminalConfig
from blue_service_terminal.session import SessionManager, rewrite_colon_sgr


def test_colon_truecolor_becomes_semicolon() -> None:
    """``38:2::r:g:b`` → ``38;2;r;g;b``。"""
    assert rewrite_colon_sgr(b"\x1b[38:2::255:199:119mX") == b"\x1b[38;2;255;199;119mX"
    # 带显式色彩空间 ID 的写法，丢掉 cs 只留 rgb
    assert rewrite_colon_sgr(b"\x1b[38:2:0:255:199:119mX") == b"\x1b[38;2;255;199;119mX"
    # 没有 cs 占位
    assert rewrite_colon_sgr(b"\x1b[38:2:255:199:119mX") == b"\x1b[38;2;255;199;119mX"


def test_background_and_256_colour_forms() -> None:
    """48（背景）与 256 色写法同样要改写。"""
    assert rewrite_colon_sgr(b"\x1b[48:2::1:2:3m") == b"\x1b[48;2;1;2;3m"
    assert rewrite_colon_sgr(b"\x1b[38:5:196m") == b"\x1b[38;5;196m"


def test_underline_style_forms() -> None:
    """下划线样式 ``4:N`` 也要改写：非 0 当开，0 当关。"""
    assert rewrite_colon_sgr(b"\x1b[4:3m") == b"\x1b[4m"
    assert rewrite_colon_sgr(b"\x1b[4:0m") == b"\x1b[24m"
    assert rewrite_colon_sgr(b"\x1b[1;4:3;31mX") == b"\x1b[1;4;31mX"


def test_plain_and_mixed_sequences_untouched() -> None:
    """没有冒号的序列一个字都不能改；带其它属性的混合参数要保序。"""
    plain = b"\x1b[1;31mRED\x1b[0m"
    assert rewrite_colon_sgr(plain) == plain
    assert rewrite_colon_sgr(b"no escapes") == b"no escapes"
    assert rewrite_colon_sgr(b"\x1b[1;38:2::9:8:7;4mX") == b"\x1b[1;38;2;9;8;7;4mX"


def test_pyte_understands_rewritten_sequence() -> None:
    """改写之后 pyte 能拿到正确的前景色，而不是把它当文本。"""
    screen = pyte.Screen(20, 2)
    stream = pyte.ByteStream(screen)

    stream.feed(rewrite_colon_sgr(b"\x1b[38:2::255:199:119mX"))

    cell = screen.buffer[0][0]
    assert cell.fg == "ffc777"
    assert cell.data == "X"
    assert screen.display[0].rstrip() == "X"  # 没有残留的参数文本


def test_session_shows_no_sgr_garbage(tmp_path: Path) -> None:
    """真实 PTY：发冒号式着色，屏幕上不能出现 ``2::255:...`` 这种残留。"""
    asyncio.run(_no_garbage(tmp_path))


async def _no_garbage(tmp_path: Path) -> None:
    # 用独立脚本输出原始字节：命令行回显里不能出现 ESC 序列，否则测不出真东西
    script = tmp_path / "paint.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'\\x1b[38:2::255:199:119mCOLORED-TEXT\\x1b[0m\\n')\n"
        "sys.stdout.flush()\n"
    )
    config = TerminalConfig()
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = manager.open(
            shell=config.resolve_shell(),
            cwd=Path("/tmp"),
            cols=100,
            rows=20,
            scrollback=200,
            prompt=config.prompt,
            isolate_rc=config.isolate_rc,
        )
        await asyncio.sleep(1.0)
        session.write(f"python3 {script}")
        await asyncio.sleep(0.8)
        text = session.text(10)
        assert "COLORED-TEXT" in text
        assert "2::255" not in text
    finally:
        await manager.stop()
