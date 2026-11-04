"""MCP stdio 客户端：JSON-RPC 2.0 over newline-delimited JSON。

只实现桥需要的三个客户端方法：``initialize`` / ``tools/list`` / ``tools/call``，
外加心跳式的错误隔离与进程树清理。

为什么不直接用官方 ``mcp`` SDK：它要拉入 opentelemetry、uvicorn、starlette、
pyjwt 等一串传递依赖，而工具进程只需要 stdio 上的这一小段协议。自带实现让
``services/mcp_bridge`` 保持零新增依赖，也让 fake server 测试不必跨版本对齐。
会话以 :class:`McpSession` 协议暴露，后续要换官方 SDK 只需替换一个实现类。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from blue.runtime import project_root
from blue_service_mcp_bridge.config import McpServerConfig

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "MAX_MESSAGE_BYTES",
    "McpAttachment",
    "McpCallError",
    "McpCallResult",
    "McpConnector",
    "McpError",
    "McpProtocolError",
    "McpSession",
    "McpTimeoutError",
    "McpToolInfo",
    "McpTransportError",
    "StdioConnector",
    "StdioMcpSession",
]

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2025-06-18"

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
"""单条 JSON-RPC 消息的读取上限。

asyncio 子进程管道的默认行缓冲只有 64 KiB：MCP server 返回一张截图
（base64 PNG）就会远超它，``readline`` 直接抛错、会话被判成「关闭了 stdout」。
这里放宽到 32 MiB，并让超限错误带上真实原因。
"""
"""客户端声明的 MCP 协议版本；服务端返回什么就按什么工作。"""

CLIENT_INFO: dict[str, str] = {
    "name": "blue-service-mcp-bridge",
    "version": "0.1.0",
}
"""握手时报告给服务端的客户端信息。"""

STDERR_TAIL = 20
"""保留的 stderr 行数，用于失败时给出一句可读的诊断。"""

CLOSE_TIMEOUT = 3.0
"""关闭会话时等待子进程退出的秒数，超时后强杀进程组。"""


class McpError(Exception):
    """MCP 交互失败的基类。"""


class McpTransportError(McpError):
    """传输层失败：进程退出、stream 关闭、写管道破裂。"""


class McpTimeoutError(McpError):
    """请求在超时预算内没有响应。

    与 :class:`McpTransportError` 分开，是因为超时通常不意味着连接已死——
    桥不应该因为一次慢调用就丢掉会话。
    """


class McpProtocolError(McpError):
    """协议层失败：响应结构不符合预期。"""


class McpCallError(McpError):
    """服务端以 JSON-RPC error 返回的失败（工具本身报错）。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        """JSON-RPC 错误码；服务端未给出时为 ``None``。"""


@dataclass(frozen=True)
class McpToolInfo:
    """服务端 ``tools/list`` 返回的一条工具。"""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True)
class McpAttachment:
    """MCP 返回的图片内容块（base64）。"""

    mime: str
    data: str
    name: str | None = None


@dataclass(frozen=True)
class McpCallResult:
    """``tools/call`` 的结果：文本摘要 + 图片附件。"""

    content: str
    is_error: bool
    attachments: tuple[McpAttachment, ...] = ()


class McpSession(Protocol):
    """一条已建立的 MCP 会话。"""

    @property
    def server_name(self) -> str:
        """该会话对应的 server 名。"""
        ...

    @property
    def is_alive(self) -> bool:
        """会话是否仍然可用。"""
        ...

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """子进程最近若干行 stderr。"""
        ...

    async def list_tools(self) -> list[McpToolInfo]:
        """列出服务端全部工具（自动翻页）。"""
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> McpCallResult:
        """调用一个工具。"""
        ...

    async def close(self) -> None:
        """关闭会话并回收子进程（幂等）。"""
        ...


class McpConnector(Protocol):
    """把 server 配置变成一个会话的工厂（测试里可替换为假实现）。"""

    async def connect(self, server: McpServerConfig) -> McpSession:
        """建立会话；失败时抛 :class:`McpError`。"""
        ...


class StdioMcpSession:
    """通过 stdio 子进程通信的 MCP 会话。

    一个会话对应一个子进程；读循环按 ``id`` 把响应派发给等待中的请求，
    服务端发来的通知直接记日志，服务端发来的请求一律以「不支持」回错，
    避免对方在等一个永远不会来的回复。
    """

    def __init__(
        self,
        *,
        server_name: str,
        process: asyncio.subprocess.Process,
        timeout: float,
    ) -> None:
        self._server_name = server_name
        self._process = process
        self._timeout = timeout
        self._pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL)
        self._server_info: dict[str, object] | None = None
        self._closed = False
        self._reader = asyncio.create_task(
            self._read_loop(), name=f"mcp.{server_name}.reader"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp.{server_name}.stderr"
        )

    @classmethod
    async def open(
        cls,
        server: McpServerConfig,
        *,
        cwd: Path,
        timeout: float,
    ) -> StdioMcpSession:
        """启动子进程并完成 ``initialize`` 握手。

        :raises McpError: 命令无法启动、握手失败或超时。子进程会被回收。
        """
        environment = dict(os.environ)
        environment.update(server.env)
        try:
            process = await asyncio.create_subprocess_exec(
                server.command,
                *server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=environment,
                start_new_session=True,
                limit=MAX_MESSAGE_BYTES,
            )
        except OSError as error:
            raise McpTransportError(
                f"无法启动 MCP server {server.name!r}（{server.command}）：{error}"
            ) from error
        session = cls(server_name=server.name, process=process, timeout=timeout)
        try:
            await session._initialize()
        except BaseException:
            await session.close()
            raise
        return session

    @property
    def server_name(self) -> str:
        """该会话对应的 server 名。"""
        return self._server_name

    @property
    def is_alive(self) -> bool:
        """进程仍在运行且会话未关闭。"""
        return not self._closed and self._process.returncode is None

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """子进程最近若干行 stderr。"""
        return tuple(self._stderr)

    @property
    def server_info(self) -> dict[str, object] | None:
        """握手时服务端报告的 ``serverInfo``（如有）。"""
        return self._server_info

    async def list_tools(self) -> list[McpToolInfo]:
        """列出服务端全部工具，按 ``nextCursor`` 自动翻页。

        :raises McpError: 任一页请求失败。
        """
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            result = await self._request("tools/list", params)
            for item in _as_list(result.get("tools")):
                info = _tool_info(item)
                if info is None:
                    logger.warning(
                        "MCP server %s 返回了无法解析的工具定义，已跳过",
                        self._server_name,
                    )
                    continue
                tools.append(info)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> McpCallResult:
        """调用一个工具并把 content 块降维成文本。

        :raises McpCallError: 服务端以 JSON-RPC error 拒绝调用。
        :raises McpTimeoutError: 超过 ``timeout``（缺省为会话默认超时）。
        :raises McpTransportError: 会话已失效或中途断开。
        """
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        text, attachments = _flatten_content(result)
        structured = result.get("structuredContent")
        if structured is not None:
            tail = json.dumps(structured, ensure_ascii=False, indent=2)
            text = f"{text}\n\n{tail}".strip()
        return McpCallResult(
            content=text,
            is_error=result.get("isError") is True,
            attachments=tuple(attachments),
        )

    async def close(self) -> None:
        """关闭 stdin、回收子进程并让等待中的请求立即失败（幂等）。"""
        if self._closed:
            return
        self._closed = True
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        for task in (self._reader, self._stderr_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._terminate()
        self._fail_pending(
            McpTransportError(f"MCP server {self._server_name!r} 会话已关闭")
        )

    async def _initialize(self) -> None:
        """``initialize`` 握手 + ``notifications/initialized``。"""
        result = await self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        version = result.get("protocolVersion")
        if isinstance(version, str) and version != DEFAULT_PROTOCOL_VERSION:
            logger.info(
                "MCP server %s 协商协议版本 %s（客户端声明 %s）",
                self._server_name,
                version,
                DEFAULT_PROTOCOL_VERSION,
            )
        server_info = result.get("serverInfo")
        if isinstance(server_info, dict):
            self._server_info = cast(dict[str, object], server_info)
        await self._notify("notifications/initialized")

    async def _request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """发一个 JSON-RPC 请求并等待其响应。"""
        if not self.is_alive:
            raise McpTransportError(self._dead_message())
        budget = self._timeout if timeout is None else timeout
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, budget)
        except TimeoutError as error:
            hint = self._stderr_hint()
            message = (
                f"MCP server {self._server_name!r} 未在 {budget:g}s 内响应 {method}"
                f"{hint}"
            )
            raise McpTimeoutError(message) from error
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str) -> None:
        """发一个 JSON-RPC 通知（不等待响应）。"""
        await self._send({"jsonrpc": "2.0", "method": method})

    async def _send(self, message: dict[str, object]) -> None:
        """把一条消息写入子进程 stdin。"""
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            raise McpTransportError(self._dead_message())
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        async with self._write_lock:
            try:
                stdin.write(payload.encode("utf-8"))
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise McpTransportError(self._dead_message()) from error

    async def _read_loop(self) -> None:
        """持续读取 stdout，把响应派发给等待中的请求。"""
        stdout = self._process.stdout
        if stdout is None:
            return
        while True:
            try:
                line = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as error:
                self._fail_pending(
                    McpTransportError(
                        f"MCP server {self._server_name!r} 单条消息超过 "
                        f"{MAX_MESSAGE_BYTES} 字节上限：{error}"
                    )
                )
                return
            except Exception:
                logger.debug(
                    "读取 MCP stdout 失败：%s", self._server_name, exc_info=True
                )
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "MCP server %s 在 stdout 输出了非 JSON 行（已忽略）：%s",
                    self._server_name,
                    text[:200],
                )
                continue
            if isinstance(message, dict):
                await self._dispatch(cast(dict[str, object], message))
        self._fail_pending(
            McpTransportError(
                f"MCP server {self._server_name!r} 关闭了 stdout{self._stderr_hint()}"
            )
        )

    async def _dispatch(self, message: dict[str, object]) -> None:
        """按消息类型分发：响应 → 等待者，通知 → 日志，服务端请求 → 回错。"""
        method = message.get("method")
        raw_id = message.get("id")
        if isinstance(method, str):
            if raw_id is None:
                logger.debug("MCP server %s 通知：%s", self._server_name, method)
                return
            await self._reject_server_request(raw_id, method)
            return
        if not isinstance(raw_id, int):
            logger.warning(
                "MCP server %s 返回了无 id 的响应，已忽略", self._server_name
            )
            return
        future = self._pending.pop(raw_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(_call_error(error))
            return
        result = message.get("result")
        future.set_result(
            cast(dict[str, object], result) if isinstance(result, dict) else {}
        )

    async def _reject_server_request(self, raw_id: object, method: str) -> None:
        """对本桥不实现的服务端请求回 ``-32601``，避免对方无限等待。"""
        with contextlib.suppress(McpTransportError):
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": raw_id,
                    "error": {
                        "code": -32601,
                        "message": f"blue MCP 桥不支持服务端方法 {method}",
                    },
                }
            )

    async def _drain_stderr(self) -> None:
        """把 stderr 收进环形缓冲，失败诊断有一句人话可看。"""
        stderr = self._process.stderr
        if stderr is None:
            return
        while True:
            try:
                line = await stderr.readline()
            except Exception:
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._stderr.append(text)
            logger.debug("[mcp:%s] %s", self._server_name, text)

    async def _terminate(self) -> None:
        """先 SIGTERM，超时后强杀整个进程组。"""
        process = self._process
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), CLOSE_TIMEOUT)
        except TimeoutError:
            _kill_tree(process)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), CLOSE_TIMEOUT)

    def _fail_pending(self, error: McpError) -> None:
        """让所有等待中的请求立即失败。"""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def _dead_message(self) -> str:
        """会话失效时的错误文本，附带退出码与 stderr 尾巴。"""
        code = self._process.returncode
        return (
            f"MCP server {self._server_name!r} 不可用"
            f"（退出码 {code}）{self._stderr_hint()}"
        )

    def _stderr_hint(self) -> str:
        """把 stderr 尾巴拼成一句诊断提示。"""
        if not self._stderr:
            return ""
        return f"；stderr: {' | '.join(tuple(self._stderr)[-3:])}"


class StdioConnector:
    """按配置启动 stdio MCP 子进程的默认连接器。"""

    def __init__(self, *, cwd: Path | None = None, timeout: float = 30.0) -> None:
        """构造连接器。

        :param cwd: 子进程默认工作目录；缺省为项目根（``blue.infd`` 所在目录）。
        :param timeout: 握手与 ``tools/list`` 的默认超时（秒）。
        """
        self._cwd = cwd
        self._timeout = timeout

    async def connect(self, server: McpServerConfig) -> McpSession:
        """启动子进程并完成握手。"""
        base = self._cwd if self._cwd is not None else project_root()
        cwd = base
        if server.cwd is not None:
            configured = Path(server.cwd).expanduser()
            cwd = configured if configured.is_absolute() else (base / configured)
        return await StdioMcpSession.open(server, cwd=cwd, timeout=self._timeout)


def _tool_info(item: object) -> McpToolInfo | None:
    """把 ``tools/list`` 的一项解析成 :class:`McpToolInfo`；非法时返回 ``None``。"""
    if not isinstance(item, dict):
        return None
    data = cast(dict[str, object], item)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = data.get("description")
    schema = data.get("inputSchema")
    return McpToolInfo(
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=cast(dict[str, object], schema)
        if isinstance(schema, dict)
        else {},
    )


def _flatten_content(
    result: dict[str, object],
) -> tuple[str, list[McpAttachment]]:
    """把 ``tools/call`` 的 content 块拆成（文本, 图片附件）。"""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return "", []
    parts: list[str] = []
    attachments: list[McpAttachment] = []
    for block in cast(list[object], blocks):
        if not isinstance(block, dict):
            continue
        text, attachment = _render_block(cast(dict[str, object], block))
        if text:
            parts.append(text)
        if attachment is not None:
            attachments.append(attachment)
    return "\n".join(parts), attachments


def _render_block(
    block: dict[str, object],
) -> tuple[str, McpAttachment | None]:
    """渲染单个 content 块：文本原样、图片转附件、其余降级为占位说明。"""
    kind = block.get("type")
    if kind is None or kind == "text":
        text = block.get("text")
        if isinstance(text, str):
            return text, None
    if kind == "image":
        data = block.get("data")
        if isinstance(data, str) and data:
            mime = _as_str(block.get("mimeType")) or "image/png"
            return "", McpAttachment(mime=mime, data=data)
    if kind == "resource":
        resource = block.get("resource")
        entry = cast(dict[str, object], resource) if isinstance(resource, dict) else {}
        uri = _as_str(entry.get("uri"))
        mime = _as_str(entry.get("mimeType"))
        blob = entry.get("blob")
        if isinstance(blob, str) and blob and mime.startswith("image/"):
            return "", McpAttachment(mime=mime, data=blob, name=uri or None)
        text = entry.get("text")
        if isinstance(text, str) and text:
            return f"[resource: {uri}]\n{text}", None
        size = _base64_size(blob)
        return f"[resource: {uri} ({mime or 'binary'}, {size} bytes)]", None
    if kind in ("audio", "resource_link", "resourceLink"):
        label = (
            f"[{kind}: {_as_str(block.get('mimeType')) or _as_str(block.get('uri'))}]"
        )
        return label, None
    return json.dumps(block, ensure_ascii=False), None


def _base64_size(data: object) -> int:
    """估算 base64 负载解码后的字节数。"""
    if not isinstance(data, str):
        return 0
    padding = data.count("=", len(data) - 2) if data.endswith("=") else 0
    return max(0, len(data) * 3 // 4 - padding)


def _call_error(error: object) -> McpCallError:
    """把 JSON-RPC error 对象转成 :class:`McpCallError`。"""
    if isinstance(error, Mapping):
        entry = cast(Mapping[object, object], error)
        message = entry.get("message")
        code = entry.get("code")
        return McpCallError(
            message
            if isinstance(message, str)
            else json.dumps(error, ensure_ascii=False),
            code=code if isinstance(code, int) else None,
        )
    return McpCallError(str(error))


def _as_str(value: object) -> str:
    """取字符串值；非字符串返回空串。"""
    return value if isinstance(value, str) else ""


def _as_list(value: object) -> list[object]:
    """把 JSON 数组视图收敛为 ``list[object]``。"""
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """强杀子进程及其进程组。"""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
