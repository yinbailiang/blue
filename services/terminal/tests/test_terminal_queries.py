"""终端查询应答：PTY 必须回答 DSR / DA / OSC，否则 TUI 会等到超时（nvim 的 E1568）。"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from blue_service_terminal import resolve_shell
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
        shell=resolve_shell(config),
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
                shell=resolve_shell(_config()),
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


_SPLITTER = (
    "import json, os, select, tty, time\n"
    "tty.setraw(0)\n"
    "os.write(1, b'\\x1b]11')\n"  # 前半段
    "time.sleep(0.05)\n"
    "os.write(1, b';?\\x07')\n"  # 后半段：同一条查询被切成两个 chunk
    "got = b''\n"
    "ready, _, _ = select.select([0], [], [], 1.5)\n"
    "while ready:\n"
    "    got += os.read(0, 4096)\n"
    "    ready, _, _ = select.select([0], [], [], 0.2)\n"
    "print(json.dumps(got.decode('latin1')))\n"
)
"""把 OSC 11 拆成两次 write 的探针：``os.read`` 不按转义序列对齐。"""


def test_split_query_is_answered(tmp_path: Path) -> None:
    """查询被读到两个 chunk 里也必须应答——漏应答就是 nvim 的 E1568。"""
    script = tmp_path / "split_ask.py"
    script.write_text(_SPLITTER)

    async def run() -> str:
        manager = SessionManager(max_sessions=2, idle_ttl=60.0)
        await manager.start()
        try:
            session = _open(manager)
            loop = asyncio.get_running_loop()
            started = loop.time() + 5
            while not session.text(5).strip() and loop.time() < started:
                await asyncio.sleep(0.02)
            session.write(f"python3 {script}")
            deadline = loop.time() + 6
            while loop.time() < deadline:
                text = session.text(10)
                if "rgb:" in text or '""' in text:
                    return text
                await asyncio.sleep(0.02)
            return session.text(10)
        finally:
            await manager.stop()

    text = asyncio.run(run())

    assert "rgb:" in text, f"被切开的查询没有得到应答：\n{text}"


@pytest.mark.skipif(NVIM is None, reason="没有安装 nvim")
def test_nvim_no_longer_reports_e1568(tmp_path: Path) -> None:
    """端到端：nvim 启动时发 OSC 11 / DSR 5，应答到位就不报 E1568。

    偶尔会撞上"应答在路上但 nvim 等不及"：机器满载时事件循环被抢占一下，nvim
    自己把这种情况叫 "Startup may be slower"。所以最多试三次——**应答路径真坏了
    每次都会报**，偶发抖动重试一次就过去了。协议级的确定性覆盖在
    ``test_pty_answers_all_queries`` 与 ``test_split_query_is_answered``。
    """
    screens: list[str] = []
    for attempt in range(3):
        screen = asyncio.run(_nvim_startup(tmp_path, attempt))
        if "E1568" not in screen:
            return
        screens.append(screen)
    pytest.fail("nvim 连续三次报 E1568（PTY 没应答查询）：\n" + "\n---\n".join(screens))


async def _until(
    check: Callable[[], bool], *, timeout: float, interval: float = 0.05
) -> bool:
    """轮询到条件成立；超时返回 ``False``。

    不要用固定 ``sleep``：全量跑时机器一忙，3 秒根本不够启动 nvim，会假失败。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return True
        await asyncio.sleep(interval)
    return check()


async def _nvim_startup(tmp_path: Path, attempt: int) -> str:
    """跑一次 nvim，返回启动后的屏幕文本（由调用方判定有没有 E1568）。"""
    manager = SessionManager(max_sessions=2, idle_ttl=60.0)
    await manager.start()
    try:
        session = _open(manager, respond_to_queries=True)
        assert await _until(lambda: bool(session.text(5).strip()), timeout=10.0), (
            "shell 没起来，无从启动 nvim"
        )
        # 每次用一个新的空文件：空 buffer 的「整屏 ~」才是可靠的就绪信号
        probe = tmp_path / f"e1568_probe_{attempt}.txt"
        session.write(f"nvim -u NONE {probe}")
        drawn = await _until(
            lambda: any(line.strip() == "~" for line in session.visible_lines()),
            timeout=30.0,
        )
        assert drawn, f"nvim 没在 30s 内画出首屏：\n{session.text(30)}"
        # E1568 和首屏同帧报（实测 0.16s）；给 1s 窗口收全再交给调用方判定
        await _until(lambda: "E1568" in session.text(30), timeout=1.0)
        return session.text(30)
    finally:
        await manager.stop()
