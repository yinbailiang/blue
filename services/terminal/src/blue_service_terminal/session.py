"""终端会话：伪终端 + VT 屏幕镜像 + 鼠标事件注入。

和 ``shell`` 工具的区别：那里是"跑一条命令、收集输出、结束"；这里是一个
**长期存活的交互式终端**——PTY 让 vim / htop / python REPL 这类全屏程序正常
工作，``pyte`` 把 ANSI 流解析成屏幕字符网格，于是可以「截屏给模型看」，
也可以按坐标把点击/拖动变成终端鼠标事件。

坐标约定：截图与点击都用**像素**（与渲染出的 PNG 一致），内部再换算成
行列；同时也接受直接给 ``col``/``row``（1 起，xterm 语义）。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyte
import wcwidth

from blue_service_terminal.colors import (
    DEFAULT_BACKGROUND,
    DEFAULT_FOREGROUND,
    to_osc_rgb,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MOUSE_BUTTONS",
    "CellStyle",
    "SessionLimitError",
    "SessionManager",
    "SessionNotFoundError",
    "TerminalSession",
    "decode_escapes",
    "rewrite_colon_sgr",
    "supports_line_editing",
]

READ_CHUNK = 65_536
"""一次从 PTY 读取的字节数。"""

RAW_TAIL_BYTES = 256 * 1024
"""保留的原始输出尾部（用于排查渲染问题）。"""

MOUSE_BUTTONS: Final[dict[str, int]] = {"left": 0, "middle": 1, "right": 2}
"""xterm 鼠标按钮编号。"""

MOUSE_MOTION_FLAG = 32
"""拖动时的 motion 标志位（SGR 协议里加在按钮码上）。"""

_MOUSE_MODE = re.compile(rb"\x1b\[\?(1000|1002|1003)(h|l)")
"""程序对鼠标跟踪的开关（DECSET/DECRST）。"""

_QUERY = re.compile(
    rb"\x1b\[(5|6)n"  # DSR 5（状态就绪）/ CPR 6（光标位置）
    rb"|\x1b\[(\??)0?c"  # DA1；\x1b[>c 是 DA2，不接管
    rb"|\x1b\]1([01]);\?(?:\x07|\x1b\\)"  # OSC 10 前景 / OSC 11 背景
)
"""程序发来的终端查询。终端必须应答，否则程序会等到超时（nvim 的 E1568）。"""

_PARTIAL_QUERY = re.compile(rb"\x1b(?:\[[0-9;?]*|\][0-9;]*)?$")
"""末尾可能是被切开的查询前缀：留到下一个 chunk 拼起来再匹配。"""

MAX_PENDING_QUERY = 64
"""跨块残片的长度上限：我们的查询最长 7 字节，更长的必然是噪声。"""

_SGR_COLON = re.compile(rb"\x1b\[([0-9;:]*:[0-9;:]*)m")
"""含冒号的 SGR（ISO-8613-6 的 ``38:2::r:g:b`` 写法）。"""


def rewrite_colon_sgr(data: bytes) -> bytes:
    """把冒号式 SGR 改成分号式。

    ``COLORTERM=truecolor`` 下 nvim 等程序用 ISO-8613-6 的冒号写法——颜色
    ``\\e[38:2::r:g:b``、下划线样式 ``\\e[4:3``——而 pyte 只认分号式，认不出就把
    剩余字符当正文，屏幕上出现 ``2::255:199:119m`` / ``3m`` 这种脏文本。
    这里统一改写；原始字节另存于 ``raw_tail``。
    """
    if b":" not in data:
        return data

    def replace(match: re.Match[bytes]) -> bytes:
        groups: list[bytes] = []
        for group in match.group(1).split(b";"):
            parts = group.split(b":")
            head = parts[0]
            if head in (b"38", b"48", b"58") and len(parts) >= 3:
                if parts[1] == b"2" and len(parts) >= 5:
                    # 丢弃色彩空间 ID，只留 r/g/b
                    groups.append(b";".join([head, b"2", *parts[-3:]]))
                    continue
                if parts[1] == b"5":
                    groups.append(b";".join([head, b"5", parts[-1]]))
                    continue
            if head == b"4" and len(parts) == 2:
                # 下划线样式 4:N → 分号式只有「开/关」两档
                groups.append(b"24" if parts[1] == b"0" else b"4")
                continue
            groups.append(group)
        return b"\x1b[" + b";".join(groups) + b"m"

    return _SGR_COLON.sub(replace, data)


ESCAPE_ALIASES: Final[dict[str, str]] = {
    "\\": "\\",
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
"""单字母转义序列（``\\e`` 即 ESC，``\\r`` 即回车）。"""

_HEX_WIDTH: Final[dict[str, int]] = {"x": 2, "u": 4, "U": 8}
"""``\\xHH`` / ``\\uXXXX`` / ``\\UXXXXXXXX`` 各自的十六进制位数。"""

_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")


def decode_escapes(text: str) -> str:
    r"""把转义写法还原成真实字符（``\u0003`` → Ctrl-C，``\e`` → ESC）。

    这样调用方不必构造真正的控制字符就能驱动 TUI：``\u001b`` 回普通模式、
    ``\u0003`` 中断前台进程、``\u001b[A`` 方向键。

    支持 ``\\``、``\e``、``\a``、``\b``、``\f``、``\n``、``\r``、``\t``、
    ``\v``、``\0``、``\xHH``、``\uXXXX``、``\UXXXXXXXX``；**无法识别或位数
    不足的序列原样保留**，所以 ``C:\Users`` 这类路径不会被吃掉。要输入字面
    反斜杠请写 ``\\``。

    :param text: 可能含转义写法的文本
    :return: 转义已展开的文本
    """
    if "\\" not in text:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        alias = ESCAPE_ALIASES.get(nxt)
        if alias is not None:
            out.append(alias)
            index += 2
            continue
        width = _HEX_WIDTH.get(nxt)
        if width is None:
            out.append(char)
            index += 1
            continue
        digits = text[index + 2 : index + 2 + width]
        if len(digits) != width or any(d not in _HEX_DIGITS for d in digits):
            out.append(char)
            index += 1
            continue
        code = int(digits, 16)
        if 0xD800 <= code <= 0xDFFF:
            # 孤立代理项编码不成 UTF-8，当成普通文本留着
            out.append(text[index : index + 2 + width])
            index += 2 + width
            continue
        out.append(chr(code))
        index += 2 + width
    return "".join(out)


class SessionNotFoundError(Exception):
    """会话不存在（或已关闭并回收）。"""


class SessionLimitError(Exception):
    """会话数已达上限。"""


@dataclass(frozen=True)
class CellStyle:
    """渲染用的一个字符格。"""

    char: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    reverse: bool = False
    cursor: bool = False


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """设置 PTY 的窗口大小（程序据此决定布局）。"""
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


class TerminalSession:
    """一个 PTY 会话：子进程 + 屏幕镜像 + 鼠标注入。"""

    def __init__(
        self,
        *,
        session_id: str,
        shell: str,
        cwd: Path,
        cols: int,
        rows: int,
        scrollback: int,
        env: dict[str, str] | None = None,
        prompt: str = "",
        isolate_rc: bool = True,
        background: str = DEFAULT_BACKGROUND,
        foreground: str = DEFAULT_FOREGROUND,
        respond_to_queries: bool = True,
    ) -> None:
        self.session_id = session_id
        self.shell = shell
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        self.last_used = self.created_at
        self.closed = False
        self.exit_code: int | None = None
        self.mouse_modes: set[str] = set()
        self.background = background
        self.foreground = foreground
        self.respond_to_queries = respond_to_queries
        self._raw: deque[int] = deque(maxlen=RAW_TAIL_BYTES)
        # 上一块末尾可能是被切开的查询前缀，拼到下一块再匹配
        self._query_pending = b""
        self._screen = pyte.HistoryScreen(cols, rows, history=scrollback)
        self._stream = pyte.ByteStream(self._screen)
        self._read_task: asyncio.Task[None] | None = None
        self._master, self._pid = self._spawn(
            shell, cwd, cols, rows, env, prompt, isolate_rc
        )
        self._reaped = False
        self._loop = asyncio.get_running_loop()
        os.set_blocking(self._master, False)
        self._loop.add_reader(self._master, self._drain)
        self._read_task = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _spawn(
        self,
        shell: str,
        cwd: Path,
        cols: int,
        rows: int,
        env: dict[str, str] | None,
        prompt: str,
        isolate_rc: bool,
    ) -> tuple[int, int]:
        """启动 shell 并返回 ``(master_fd, pid)``。"""
        master, slave = pty.openpty()
        _set_winsize(slave, rows, cols)
        environment = dict(os.environ)
        environment.setdefault("TERM", "xterm-256color")
        environment.setdefault("COLORTERM", "truecolor")
        environment["COLUMNS"] = str(cols)
        environment["LINES"] = str(rows)
        if prompt:
            # 固定提示符：用户自己的 PS1 常带颜色转义，截图里会变成噪声
            environment["PS1"] = prompt
            environment["PROMPT"] = prompt
        if env:
            environment.update(env)
        pid = os.fork()
        if pid == 0:  # pragma: no cover - 子进程分支
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                os.chdir(cwd)
                argv = _shell_argv(shell, isolate_rc=isolate_rc)
                os.execvpe(argv[0], argv, environment)
            except BaseException:
                os._exit(127)
        os.close(slave)
        return master, pid

    def _drain(self) -> None:
        """PTY 可读：把数据喂给 pyte，并记录原始尾部。"""
        while True:
            try:
                chunk = os.read(self._master, READ_CHUNK)
            except BlockingIOError:
                return
            except OSError:
                self._mark_closed()
                return
            if not chunk:
                self._mark_closed()
                return
            self._raw.extend(chunk)
            for match in _MOUSE_MODE.finditer(chunk):
                mode = match.group(1).decode()
                if match.group(2) == b"h":
                    self.mouse_modes.add(mode)
                else:
                    self.mouse_modes.discard(mode)
            try:
                self._stream.feed(rewrite_colon_sgr(chunk))
            except Exception:
                logger.warning(
                    "终端 %s 的 VT 解析失败（已跳过该块）",
                    self.session_id,
                    exc_info=True,
                )
            # 放在 feed 之后：CPR 要回的是「程序发问那一刻」的光标位置
            self._answer_queries(chunk)

    def _answer_queries(self, chunk: bytes) -> None:
        """应答程序发出的终端查询；不应答会让 nvim 之类等到超时报 E1568。

        ``os.read`` **不按转义序列对齐**：``ESC ] 11 ; ? BEL`` 完全可能被切成两个
        chunk（实测能稳定复现）。所以上一块末尾的残片要留到这一块拼起来再匹配，
        否则那条查询一条也匹配不到——nvim 就报 E1568。
        """
        if not self.respond_to_queries:
            return
        data = self._query_pending + chunk
        self._query_pending = b""
        replies = [
            reply
            for match in _QUERY.finditer(data)
            if (reply := self._query_reply(match))
        ]
        if replies:
            try:
                self._write_bytes(b"".join(replies))
            except TimeoutError, SessionNotFoundError:
                logger.debug("终端 %s 的查询应答写入失败（已忽略）", self.session_id)
        partial = _PARTIAL_QUERY.search(data)
        if partial is not None and len(partial.group(0)) <= MAX_PENDING_QUERY:
            self._query_pending = partial.group(0)

    def _query_reply(self, match: re.Match[bytes]) -> bytes:
        """按查询类型给出应答；不认识的返回空字节。"""
        kind = match.group(1)
        if kind == b"5":
            return b"\x1b[0n"  # 「我还在，一切正常」
        if kind == b"6":
            col, row = self.cursor
            return f"\x1b[{row + 1};{col + 1}R".encode()
        if match.group(2) is not None:
            return b"\x1b[?1;2c"  # DA1：VT100 with Advanced Video
        is_foreground = match.group(3) == b"0"
        color = self.foreground if is_foreground else self.background
        code = 10 if is_foreground else 11
        return f"\x1b]{code};{to_osc_rgb(color)}\x1b\\".encode()

    def _mark_closed(self) -> None:
        """对端结束：摘掉 reader、回收子进程、记下退出码。"""
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self._loop.remove_reader(self._master)
        with contextlib.suppress(ChildProcessError, OSError):
            reaped, status = os.waitpid(self._pid, os.WNOHANG)
            if reaped == self._pid:
                self._reaped = True
                if os.WIFEXITED(status):
                    self.exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    self.exit_code = -os.WTERMSIG(status)
        self.touch()

    def close(self) -> None:
        """杀掉进程组并释放 fd（幂等）。

        只按 ``self._pid`` 发信号：子进程启动时已经 ``setsid``，它的 pgid 就是
        自己的 pid。**不要**用 ``os.getpgid(pid)`` —— 子进程已被回收时那个 pid
        可能落到别的进程组上，一不小心就把自己的进程组杀了（实测会让 pytest
        整个进程组收到 SIGHUP 而退出）。
        """
        if not self.closed and not self._reaped:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._pid, signal.SIGHUP)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._pid, signal.SIGTERM)
            self._mark_closed()
        with contextlib.suppress(OSError):
            os.close(self._master)

    def touch(self) -> None:
        """刷新最近使用时间。"""
        self.last_used = time.time()

    @property
    def alive(self) -> bool:
        """PTY 是否还活着。"""
        return not self.closed

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------

    def write(self, text: str, *, newline: bool = True, literal: bool = False) -> None:
        r"""把文本写进 PTY（``newline`` 表示补一个回车）。

        默认先由 :func:`decode_escapes` 展开转义写法，因此
        ``write("\\u0003")`` 送出的是一个真正的 Ctrl-C 字节，而不是 6 个字符。

        ``literal=True`` 跳过展开，按字面写入。往编辑器里贴含反斜杠的代码
        （``print("a\nb")``、``C:\Users\...``）时必须用它，否则 ``\n`` 会变成真换行。
        """
        body = text if literal else decode_escapes(text)
        self._write_bytes((body + ("\r" if newline else "")).encode("utf-8"))

    def send(self, data: bytes) -> None:
        """直接写原始字节（转义序列、控制字符），不做转义展开。"""
        self._write_bytes(data)

    def _write_bytes(self, data: bytes) -> None:
        """把字节写进 PTY（非阻塞 + 超时）。"""
        deadline = time.monotonic() + 2.0
        while data:
            try:
                written = os.write(self._master, data)
                data = data[written:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("写入终端超时") from None
                time.sleep(0.002)
            except OSError as error:
                raise SessionNotFoundError(f"终端已关闭：{self.session_id}") from error
        self.touch()

    # ------------------------------------------------------------------
    # 鼠标
    # ------------------------------------------------------------------

    def click(
        self, col: int, row: int, *, button: str = "left", count: int = 1
    ) -> None:
        """在 ``(col, row)``（1 起）发送点击（SGR 1006 协议）。"""
        code = MOUSE_BUTTONS.get(button)
        if code is None:
            raise ValueError(f"不支持的按钮：{button}")
        col = max(1, min(col, self.cols))
        row = max(1, min(row, self.rows))
        for _ in range(max(1, count)):
            self._mouse(code, col, row, final="M")
            self._mouse(code, col, row, final="m")

    def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        button: str = "left",
        steps: int = 8,
    ) -> None:
        """按住按钮从 ``start`` 拖到 ``end``（行列，1 起）。"""
        code = MOUSE_BUTTONS.get(button)
        if code is None:
            raise ValueError(f"不支持的按钮：{button}")
        steps = max(1, steps)
        start_col, start_row = _clamp(start, self.cols, self.rows)
        end_col, end_row = _clamp(end, self.cols, self.rows)
        self._mouse(code, start_col, start_row, final="M")
        for index in range(1, steps + 1):
            col = round(start_col + (end_col - start_col) * index / steps)
            row = round(start_row + (end_row - start_row) * index / steps)
            self._mouse(code + MOUSE_MOTION_FLAG, col, row, final="M")
        self._mouse(code, end_col, end_row, final="m")

    def _mouse(self, code: int, col: int, row: int, *, final: str) -> None:
        """发送一个 SGR 鼠标事件。"""
        self.send(f"\x1b[<{code};{col};{row}{final}".encode())

    @property
    def mouse_tracking(self) -> bool:
        """程序是否启用了鼠标跟踪。"""
        return bool(self.mouse_modes)

    # ------------------------------------------------------------------
    # 屏幕
    # ------------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        """调整终端大小（同时通知子进程）。"""
        self.cols = cols
        self.rows = rows
        _set_winsize(self._master, rows, cols)
        with contextlib.suppress(Exception):
            self._screen.resize(rows, cols)
        self.touch()

    def visible_lines(self) -> list[str]:
        """当前屏幕的可见文本行。"""
        return list(self._screen.display)

    def recent_lines(self, lines: int) -> list[str]:
        """最近 ``lines`` 行（含回滚历史），用于读命令输出。

        先裁掉屏幕底部的连续空行：内容靠上时（例如 python REPL 刚启动），
        直接取「最后 N 行」会全是空白。
        """
        display = self.visible_lines()
        while display and not display[-1].strip():
            display.pop()
        if lines <= len(display):
            return display[-lines:]
        wanted = lines - len(display)
        history: list[str] = []
        for row in list(self._screen.history.top)[-wanted:]:
            history.append("".join(row[x].data for x in sorted(row)).rstrip())
        return [*history, *display]

    def text(self, lines: int) -> str:
        """最近若干行的纯文本（右侧空白与尾部空行裁掉）。"""
        rows = self.recent_lines(lines)
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(line.rstrip() for line in rows)

    @property
    def cursor(self) -> tuple[int, int]:
        """光标位置（0 起的列、行）。"""
        return self._screen.cursor.x, self._screen.cursor.y

    @property
    def cursor_hidden(self) -> bool:
        """光标是否被程序隐藏。"""
        return bool(self._screen.cursor.hidden)

    def cells(self) -> list[list[CellStyle]]:
        """把屏幕转成渲染用的字符格（宽字符占两格）。"""
        grid: list[list[CellStyle]] = []
        cursor_x, cursor_y = self.cursor
        show_cursor = not self.cursor_hidden and not self.closed
        for y in range(self.rows):
            row = self._screen.buffer[y]
            cells: list[CellStyle] = []
            x = 0
            while x < self.cols:
                char = row[x]
                width = wcwidth.wcwidth(char.data[0]) if char.data else 1
                if width <= 0:
                    # 宽字符的右半格：跳过（渲染时由前一个格覆盖）
                    x += 1
                    continue
                cells.append(
                    CellStyle(
                        char=char.data or " ",
                        fg=char.fg,
                        bg=char.bg,
                        bold=char.bold,
                        reverse=char.reverse,
                        cursor=show_cursor and x == cursor_x and y == cursor_y,
                    )
                )
                if width == 2:
                    cells.append(CellStyle(char="", fg=char.fg, bg=char.bg))
                x += 1 if width == 1 else width
            grid.append(cells)
        return grid

    def raw_tail(self, limit: int = 4000) -> str:
        """原始输出尾部（转义序列原样），用于排查渲染差异。"""
        data = bytes(self._raw)[-limit:]
        return data.decode("utf-8", errors="replace")


class SessionManager:
    """会话池：开、取、列、关，以及空闲回收。"""

    def __init__(self, *, max_sessions: int, idle_ttl: float) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._max = max_sessions
        self._idle_ttl = idle_ttl
        self._counter = 0
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动空闲回收任务。"""
        self._reaper = asyncio.create_task(self._reap_loop(), name="terminal.reaper")

    async def stop(self) -> None:
        """关闭全部会话并停止回收任务。"""
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()

    def open(
        self,
        *,
        shell: str,
        cwd: Path,
        cols: int,
        rows: int,
        scrollback: int,
        env: dict[str, str] | None = None,
        prompt: str = "",
        isolate_rc: bool = True,
        background: str = DEFAULT_BACKGROUND,
        foreground: str = DEFAULT_FOREGROUND,
        respond_to_queries: bool = True,
    ) -> TerminalSession:
        """新建一个会话。

        :raises SessionLimitError: 已有会话达到上限
        """
        self._prune_dead()
        if len(self._sessions) >= self._max:
            raise SessionLimitError(
                f"会话数已达上限（{self._max}），先 terminal_close 掉不用的"
            )
        self._counter += 1
        session_id = f"t{self._counter}"
        session = TerminalSession(
            session_id=session_id,
            shell=shell,
            cwd=cwd,
            cols=cols,
            rows=rows,
            scrollback=scrollback,
            env=env,
            prompt=prompt,
            isolate_rc=isolate_rc,
            background=background,
            foreground=foreground,
            respond_to_queries=respond_to_queries,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> TerminalSession:
        """按 id 取会话。

        :raises SessionNotFoundError: 不存在
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"没有这个终端会话：{session_id}")
        session.touch()
        return session

    def list(self) -> list[TerminalSession]:
        """全部会话（按创建顺序）。"""
        return list(self._sessions.values())

    def close(self, session_id: str) -> bool:
        """关闭并移除一个会话；返回是否确实存在。"""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    def _prune_dead(self) -> None:
        """移除已经结束的会话，给新会话腾位置。"""
        for session_id, session in list(self._sessions.items()):
            if session.closed:
                self._sessions.pop(session_id, None)

    async def _reap_loop(self) -> None:
        """定期回收空闲太久的会话。"""
        while True:
            await asyncio.sleep(min(60.0, max(5.0, self._idle_ttl / 10)))
            now = time.time()
            for session_id, session in list(self._sessions.items()):
                if session.alive and now - session.last_used < self._idle_ttl:
                    continue
                logger.info(
                    "回收终端会话 %s（空闲 %ss）",
                    session_id,
                    int(now - session.last_used),
                )
                self.close(session_id)


def _clamp(point: tuple[int, int], cols: int, rows: int) -> tuple[int, int]:
    """把行列限制在屏幕内。"""
    col, row = point
    return max(1, min(col, cols)), max(1, min(row, rows))


def supports_line_editing(shell: str) -> bool | None:
    r"""shell 是否支持行编辑（方向键、历史、``\[ \]`` 提示符标记）。

    NixOS 把 bash 分两个构建：``bash``（不带 readline）与 ``bash-interactive``。
    PATH 先命中的往往是前者，于是出现「提示符里的 ``\[\]`` 原样打印、方向键
    完全无效」——这跟提示符配置无关，是**这个 bash 压根没有行编辑**。

    :return: ``True``/``False`` 表示支持/不支持；``None`` 表示无法判断（非 bash）
    """
    import shlex
    import subprocess

    parts = shlex.split(shell)
    if not parts:
        return None
    if Path(parts[0]).name not in {"bash", "rbash"}:
        return None
    try:
        completed = subprocess.run(
            [*parts, "-c", "bind -v"],
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def _shell_argv(shell: str, *, isolate_rc: bool) -> list[str]:
    """把 ``shell`` 拆成 argv（允许 ``bash -l`` / ``python3 -i``）。

    ``isolate_rc`` 时给 bash/zsh 关掉用户 rc：它们常把 PS1 设成带颜色转义的
    提示符，截图里就是噪声，而且环境不可复现。
    """
    import shlex

    parts = shlex.split(shell)
    if not parts:
        raise ValueError("shell 不能为空")
    if not isolate_rc:
        return parts
    name = Path(parts[0]).name
    if name == "bash":
        parts[1:1] = ["--norc", "--noprofile"]
    elif name == "zsh":
        parts[1:1] = ["--no-rcs"]
    return parts
