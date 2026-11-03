"""MCP 服务端桥：把 blue 工具目录暴露成 MCP server（stdio）。

外部 MCP 客户端（Claude Code / Cursor / Claude Desktop）启动本进程后：

```text
tools/list  ←  catalog.query（按 expose 白名单过滤）
tools/call  →  tool.call（总线 RPC）→ tool.result  →  content 块 + isError
```

约束：

- **stdout 只允许协议帧**，日志与诊断一律走 stderr；
- 请求并发处理，慢工具不会堵住 ``ping`` / ``tools/list``；
- 任何失败都回 JSON-RPC 结果或错误，绝不让客户端挂在半空；
- 白名单是必需的（不支持「全部」），危险工具要额外放行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, cast

from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from infinity_bus.templates.request import request

from blue.transport import BusRuntime
from blue_service_mcp_server.config import (
    DEFAULT_PROTOCOL_VERSION,
    McpServerSettings,
)
from blue_service_mcp_server.schema import to_json_schema

logger = logging.getLogger(__name__)

__all__ = [
    "McpStdioServer",
    "MessageIO",
    "StdioMessageIO",
]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
"""JSON-RPC 2.0 错误码。"""


class MessageIO(Protocol):
    """行式消息通道（生产用 stdio，测试用内存实现）。"""

    async def read_line(self) -> str | None:
        """读一行；EOF 返回 ``None``。"""
        ...

    async def write_line(self, text: str) -> None:
        """写一行（不含换行符）。"""
        ...


class StdioMessageIO:
    """stdin / stdout 行式通道。

    读操作放到线程里，避免阻塞事件循环；stdout 只承载协议帧。
    """

    async def read_line(self) -> str | None:
        """从 stdin 读一行；EOF 返回 ``None``。"""
        line = await asyncio.to_thread(sys.stdin.readline)
        return None if line == "" else line

    async def write_line(self, text: str) -> None:
        """向 stdout 写一行并立即刷新。"""
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()


class _RpcError(Exception):
    """内部用：把处理失败映射成 JSON-RPC error。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpStdioServer:
    """把 blue 工具目录暴露成 stdio MCP server。

    实现 ``LifecycleService`` 之外的最小形态：``serve`` 自己管理总线的启动与关闭。
    """

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        settings: McpServerSettings,
        session_id: str | None = None,
    ) -> None:
        """构造服务端桥。

        :param runtime: 已装配好的总线运行时（``serve`` 时启动）。
        :param settings: 白名单、超时与握手信息。
        :param session_id: ``tool.call`` 信封里使用的会话标识；缺省自动生成。
        """
        self._runtime = runtime
        self._settings = settings
        self._session_id = session_id or f"mcp-server.{uuid.uuid4().hex[:8]}"
        self._write_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[None]] = set()

    @property
    def session_id(self) -> str:
        """``tool.call`` 信封里使用的会话标识。"""
        return self._session_id

    @property
    def exposed(self) -> tuple[str, ...]:
        """白名单（顺序即声明顺序）。"""
        return tuple(self._settings.expose)

    async def serve(self, io: MessageIO) -> None:
        """读请求直到 EOF，再等所有在途请求写完响应。

        并发处理每个请求：一个慢工具调用不会堵住 ``ping`` 或 ``tools/list``。
        """
        await self._runtime.start()
        try:
            while True:
                line = await io.read_line()
                if line is None:
                    break
                text = line.strip()
                if not text:
                    continue
                self._spawn(io, text)
            await self._drain()
        finally:
            await self._runtime.stop()

    def _spawn(self, io: MessageIO, text: str) -> None:
        """为一条入站消息起一个任务。"""
        task = asyncio.create_task(
            self._handle_line(io, text), name="mcp-server.request"
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _drain(self) -> None:
        """等所有在途请求结束。"""
        while self._inflight:
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)

    async def _handle_line(self, io: MessageIO, text: str) -> None:
        """解析并处理一行消息，保证「有请求必有响应或有日志」。"""
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(io, None, PARSE_ERROR, "无法解析的 JSON")
            return
        if not isinstance(message, dict):
            await self._send_error(io, None, INVALID_REQUEST, "消息必须是 JSON 对象")
            return
        envelope = cast(dict[str, Any], message)
        raw_id = envelope.get("id")
        method = envelope.get("method")
        if not isinstance(method, str):
            # 客户端对本进程请求的响应：本桥不发起请求，忽略即可。
            logger.debug("忽略非请求消息：%s", text[:120])
            return
        if raw_id is None:
            self._handle_notification(method, envelope.get("params"))
            return
        try:
            result = await self._handle_request(method, envelope.get("params"))
        except _RpcError as error:
            await self._send_error(io, raw_id, error.code, error.message)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("处理 MCP 请求 %s 时出现未预期错误", method)
            await self._send_error(io, raw_id, INTERNAL_ERROR, str(error))
            return
        await self._send_result(io, raw_id, result)

    def _handle_notification(self, method: str, params: object) -> None:
        """处理客户端通知（当前只有日志语义）。"""
        if method == "notifications/cancelled":
            logger.info("客户端请求取消（无法传播到工具进程）：%s", params)
            return
        logger.debug("客户端通知：%s", method)

    async def _handle_request(self, method: str, params: object) -> dict[str, Any]:
        """分发请求。"""
        payload = cast(dict[str, Any], params) if isinstance(params, dict) else {}
        if method == "initialize":
            return self._initialize(payload)
        if method == "ping":
            return {}
        if method == "tools/list":
            return await self._list_tools()
        if method == "tools/call":
            return await self._call_tool(payload)
        raise _RpcError(METHOD_NOT_FOUND, f"未实现的方法 {method!r}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：回显客户端协议版本（不认识则用自己的缺省值）。"""
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) else DEFAULT_PROTOCOL_VERSION
        client_info = params.get("clientInfo")
        if isinstance(client_info, Mapping):
            logger.info(
                "MCP 客户端已连接：%s", dict(cast(Mapping[str, Any], client_info))
            )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self._settings.server_name,
                "version": self._settings.server_version,
            },
        }

    async def _list_tools(self) -> dict[str, Any]:
        """按白名单过滤工具目录并转换 schema。"""
        catalog = await self._query_catalog()
        allowed = set(self.exposed)
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for definition in catalog.tools:
            name = definition.function.name
            if name not in allowed:
                continue
            seen.add(name)
            tools.append(
                {
                    "name": name,
                    "description": definition.function.description
                    or f"{name}（blue 工具）",
                    "inputSchema": to_json_schema(definition.function.parameters),
                }
            )
        missing = sorted(allowed - seen)
        if missing:
            logger.warning(
                "白名单里的工具当前不在目录中（对应服务未运行？）：%s", missing
            )
        return {"tools": tools}

    async def _query_catalog(self) -> CatalogQueryResponse:
        """向注册中心取目录快照。"""
        try:
            response = await request(
                self._runtime.proxy(),
                CatalogQueryEvent.name,
                {},
                CatalogReplyEvent.name,
                session_id=self._session_id,
                timeout=self._settings.timeout,
            )
        except TimeoutError as error:
            raise _RpcError(
                INTERNAL_ERROR,
                f"注册中心在 {self._settings.timeout:g}s 内没有响应"
                "（registry 服务未运行？）",
            ) from error
        if not isinstance(response, CatalogQueryResponse):
            raise _RpcError(INTERNAL_ERROR, "收到非预期的目录响应类型")
        return response

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """把 ``tools/call`` 转成 ``tool.call``，结果映射回 content 块。

        调用前先核对目录：白名单里有、但当前目录里没有的工具（对应服务没运行、
        或写错了工具名）立即失败，而不是一直等到超时——这正是「没有应答的
        ``tool.call``」最典型的来源。
        """
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _RpcError(INVALID_PARAMS, "tools/call 缺少工具名")
        if name not in set(self.exposed):
            raise _RpcError(INVALID_PARAMS, f"工具 {name!r} 未在 expose 白名单中")
        catalog = await self._query_catalog()
        available = {tool.function.name for tool in catalog.tools}
        if name not in available:
            raise _RpcError(
                INVALID_PARAMS,
                f"工具 {name!r} 当前不在目录中（对应工具服务未运行？）"
                f"可用工具：{sorted(available)}",
            )
        arguments = params.get("arguments")
        payload: dict[str, Any] = (
            dict(cast(dict[str, Any], arguments)) if isinstance(arguments, dict) else {}
        )
        try:
            response = await request(
                self._runtime.proxy(),
                ToolCallEvent.name,
                {"tool": name, "arguments": payload},
                ToolResultEvent.name,
                session_id=self._session_id,
                timeout=self._settings.timeout,
            )
        except TimeoutError as error:
            raise _RpcError(
                INTERNAL_ERROR,
                f"工具 {name!r} 在 {self._settings.timeout:g}s 内没有响应"
                "（对应工具服务可能未运行）",
            ) from error
        if not isinstance(response, ToolCallResult):
            raise _RpcError(INTERNAL_ERROR, "收到非预期的工具响应类型")
        text = response.content or response.error_msg or ""
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not response.success,
        }

    async def _send_result(
        self, io: MessageIO, raw_id: object, result: dict[str, Any]
    ) -> None:
        """写一条 JSON-RPC 成功响应。"""
        await self._write(io, {"jsonrpc": "2.0", "id": raw_id, "result": result})

    async def _send_error(
        self, io: MessageIO, raw_id: object, code: int, message: str
    ) -> None:
        """写一条 JSON-RPC 错误响应。"""
        await self._write(
            io,
            {
                "jsonrpc": "2.0",
                "id": raw_id,
                "error": {"code": code, "message": message},
            },
        )

    async def _write(self, io: MessageIO, message: dict[str, Any]) -> None:
        """串行写出（避免并发任务交错写坏一行）。"""
        text = json.dumps(message, ensure_ascii=False)
        async with self._write_lock:
            await io.write_line(text)
