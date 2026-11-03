"""MCP 客户端桥：把外部 MCP server 的工具接入 blue 工具目录。

行为要点：

- 工具集是**声明式**的：注册哪些工具由 ``blue.infd`` 的 ``tools`` 白名单决定，
  不随 MCP 的 ``tools/list_changed`` 变化；运行期只做重连与探活。
- 声明的工具在 server 上不存在 → 启动失败（fail loud），与密钥缺失的处理一致：
  少一个工具比静默少一个工具好查。
- 调用失败一律回 ``tool.result(success=False)``，绝不让调用方空等：
  没有应答的 ``tool.call`` 会一直挂到 agent 的 ``tool_timeout``，并在会话里留下
  没有结果的 ``tool_calls``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from blue_contracts_tool import ToolCallRequest, ToolCallResult, ToolResultEvent
from infinity_agent import ToolDefinition, ToolFunction
from infinity_bus import EventBus

from blue.control.backoff import Backoff
from blue.subscribers.tool_call import ToolCallHandler
from blue.transport import BusRuntime
from blue.worker import ToolLease, ToolRegistrar
from blue_service_mcp_bridge.client import (
    McpCallError,
    McpConnector,
    McpError,
    McpSession,
    McpTimeoutError,
    McpToolInfo,
    McpTransportError,
    StdioConnector,
)
from blue_service_mcp_bridge.config import (
    McpBridgeConfig,
    McpServerConfig,
    parse_tool_name,
    tool_name,
    worker_id,
)
from blue_service_mcp_bridge.schema import convert_input_schema

logger = logging.getLogger(__name__)

__all__ = ["BoundTool", "McpBridge"]


@dataclass(frozen=True)
class BoundTool:
    """一条「blue 工具 → MCP 工具」的绑定。"""

    server: str
    remote: str
    definition: ToolDefinition
    worker_id: str

    @property
    def name(self) -> str:
        """blue 侧工具名。"""
        return self.definition.function.name


class McpBridge:
    """把一个或多个 MCP server 的工具登记进 blue 工具目录。

    实现 ``LifecycleService`` 协议，可直接交给 ``blue.runtime.run_service``。
    """

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        config: McpBridgeConfig,
        ttl: float = 15.0,
        connector: McpConnector | None = None,
    ) -> None:
        """构造桥。

        :param runtime: 已装配好的总线运行时（:meth:`start` 时启动）。
        :param config: 桥配置（server 列表 + 工具白名单 + 超时）。
        :param ttl: 工具租约时长（秒）。
        :param connector: 会话工厂；缺省为 stdio 连接器（测试可替换）。
        """
        self._runtime = runtime
        self._config = config
        self._connector: McpConnector = connector or StdioConnector(
            timeout=config.startup_timeout
        )
        self._ttl = ttl
        self._sessions: dict[str, McpSession] = {}
        self._remote: dict[str, dict[str, McpToolInfo]] = {}
        self._tools: dict[str, BoundTool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._attempts: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._backoff = Backoff(
            base=config.reconnect_backoff, maximum=config.reconnect_backoff_max
        )
        self._registrar: ToolRegistrar | None = None
        self._supervisor: asyncio.Task[None] | None = None

    @property
    def tools(self) -> dict[str, BoundTool]:
        """已绑定的工具（blue 工具名 → 绑定）。"""
        return dict(self._tools)

    async def start(self) -> None:
        """连总线、连接全部 server、按白名单登记工具。

        :raises McpError: 连接失败、声明的工具不存在或配置自相矛盾。
        """
        await self._runtime.start()
        try:
            await self._runtime.register(ToolCallHandler(self))
            await self._discover()
        except BaseException:
            await self._shutdown_sessions()
            await self._runtime.stop()
            raise
        self._registrar = ToolRegistrar(
            runtime=self._runtime,
            leases=[
                ToolLease(worker_id=bound.worker_id, definition=bound.definition)
                for bound in self._tools.values()
            ],
            ttl=self._ttl,
        )
        await self._registrar.announce()
        self._supervisor = asyncio.create_task(
            self._supervise_loop(), name="mcp-bridge.supervise"
        )
        logger.info(
            "MCP 桥已上线：%s 个 server、%s 个工具（%s）",
            len(self._sessions),
            len(self._tools),
            ", ".join(sorted(self._tools)) or "无",
        )

    async def stop(self) -> None:
        """注销全部工具、关闭全部会话、停总线（幂等）。"""
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        registrar, self._registrar = self._registrar, None
        if registrar is not None:
            await registrar.withdraw()
        await self._shutdown_sessions()
        if self._runtime.is_running:
            await self._runtime.stop()
        logger.info("MCP 桥已下线")

    async def handle_call(
        self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """处理一次工具调用；不是本桥的工具则直接忽略。

        任何失败路径都以 ``success=False`` 的 ``tool.result`` 收尾。
        """
        bound = self._tools.get(payload.tool)
        if bound is None:
            return
        content, ok = await self._invoke(bound, dict(payload.arguments))
        await bus_proxy.publish(
            ToolResultEvent.name,
            ToolCallResult(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=ok,
                error_msg=None if ok else content,
                content=content,
            ),
        )

    async def _invoke(
        self, bound: BoundTool, arguments: dict[str, object]
    ) -> tuple[str, bool]:
        """执行一次 MCP 调用，返回（文本, 是否成功）。"""
        session = await self._session_for(bound.server)
        if session is None:
            return self._failure(bound, f"MCP server {bound.server!r} 不可用")
        try:
            result = await session.call_tool(
                bound.remote, arguments, timeout=self._config.call_timeout
            )
        except McpTimeoutError as error:
            return self._failure(bound, f"调用超时：{error}")
        except McpCallError as error:
            return self._failure(bound, f"MCP server 返回错误：{error}")
        except McpTransportError as error:
            await self._drop_session(bound.server)
            return self._failure(bound, f"MCP server 通信中断：{error}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("MCP 工具 %s 调用异常", bound.name)
            return self._failure(bound, f"调用异常：{error}")
        if result.is_error:
            return result.content or f"MCP 工具 {bound.name} 返回 isError", False
        return result.content, True

    def _failure(self, bound: BoundTool, reason: str) -> tuple[str, bool]:
        """记录并格式化一次失败。"""
        logger.warning("MCP 工具 %s 调用失败：%s", bound.name, reason)
        return f"MCP 工具 {bound.name}：{reason}", False

    async def _discover(self) -> None:
        """连接全部启用的 server，并按白名单绑定工具。"""
        enabled = self._config.enabled_servers
        if not enabled:
            if self._config.servers:
                logger.warning("全部 MCP server 都被禁用，桥将以空目录运行")
            else:
                logger.info("未声明任何 MCP server，桥以空目录运行")
            return
        for server in enabled:
            await self._connect(server)
        self._bind_tools()

    def _bind_tools(self) -> None:
        """把白名单里的工具绑定成 blue 工具定义。

        :raises McpError: 白名单为空但声明了 server，或有工具在 server 上不存在。
        """
        if not self._config.tools:
            if self._sessions:
                raise McpError(
                    "已声明 MCP server 但没有声明任何工具："
                    "请在 tools 里列出 <server>__<tool>"
                )
            return

        missing: list[str] = []
        for entry in self._config.tools:
            parsed = parse_tool_name(entry)
            if parsed is None:
                raise McpError(f"工具名 {entry!r} 格式非法")
            server, remote = parsed
            info = self._remote.get(server, {}).get(remote)
            if info is None:
                missing.append(entry)
                continue
            conversion = convert_input_schema(info.input_schema)
            for note in conversion.notes:
                logger.warning("MCP 工具 %s 的参数 schema 降级：%s", entry, note)
            description = info.description.strip() or (
                f"{server} MCP server 提供的工具 {remote}"
            )
            definition = ToolDefinition(
                function=ToolFunction(
                    name=entry,
                    description=conversion.describe(description),
                    parameters=conversion.parameters,
                )
            )
            self._tools[entry] = BoundTool(
                server=server,
                remote=remote,
                definition=definition,
                worker_id=worker_id(server, remote),
            )

        if missing:
            raise McpError(
                "以下声明的工具在对应 MCP server 上不存在："
                + "、".join(sorted(missing))
            )

        declared = {(bound.server, bound.remote) for bound in self._tools.values()}
        for server, tools in self._remote.items():
            for name in tools:
                if (server, name) not in declared:
                    logger.warning(
                        "MCP 工具 %s 未被声明，不会注册到目录",
                        tool_name(server, name),
                    )

    async def _connect(self, server: McpServerConfig) -> None:
        """建立一条新会话并缓存它的工具清单。"""
        session = await self._connector.connect(server)
        try:
            tools = await session.list_tools()
        except BaseException:
            await session.close()
            raise
        self._sessions[server.name] = session
        self._remote[server.name] = {tool.name: tool for tool in tools}
        self._locks.setdefault(server.name, asyncio.Lock())
        self._attempts[server.name] = 0
        self._retry_at.pop(server.name, None)
        logger.info("已连接 MCP server %s：%s 个工具", server.name, len(tools))

    async def _session_for(self, server_name: str) -> McpSession | None:
        """取一个可用会话；会话已死时尝试一次重连。"""
        session = self._sessions.get(server_name)
        if session is not None and session.is_alive:
            return session
        return await self._reconnect(server_name)

    async def _reconnect(self, server_name: str) -> McpSession | None:
        """按退避策略重连一个 server；失败返回 ``None``（调用方立即失败）。"""
        config = self._config.server(server_name)
        if config is None:
            return None
        lock = self._locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            current = self._sessions.get(server_name)
            if current is not None and current.is_alive:
                return current
            if current is not None:
                await self._drop_session(server_name)
            now = time.monotonic()
            if now < self._retry_at.get(server_name, 0.0):
                return None
            try:
                await self._connect(config)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempt = self._attempts.get(server_name, 0) + 1
                self._attempts[server_name] = attempt
                delay = self._backoff.delay(attempt)
                self._retry_at[server_name] = time.monotonic() + delay
                logger.warning(
                    "重连 MCP server %s 失败（第 %s 次，%.1fs 后可再试）：%s",
                    server_name,
                    attempt,
                    delay,
                    error,
                )
                return None
            logger.info("MCP server %s 已重连", server_name)
            self._warn_missing_tools(server_name)
            return self._sessions.get(server_name)

    def _warn_missing_tools(self, server_name: str) -> None:
        """重连后提示「声明过但 server 不再提供」的工具（目录保持不变）。"""
        remote = self._remote.get(server_name, {})
        for bound in self._tools.values():
            if bound.server == server_name and bound.remote not in remote:
                logger.warning(
                    "重连后 MCP server %s 不再提供工具 %s；目录不变，调用会失败",
                    server_name,
                    bound.remote,
                )

    async def _drop_session(self, server_name: str) -> None:
        """关闭并移除一个 server 的会话。"""
        session = self._sessions.pop(server_name, None)
        if session is not None:
            await session.close()

    async def _supervise_loop(self) -> None:
        """周期探活：会话死了就按退避重连。"""
        while True:
            await asyncio.sleep(self._config.health_interval)
            for server in self._config.enabled_servers:
                session = self._sessions.get(server.name)
                if session is not None and session.is_alive:
                    continue
                try:
                    await self._reconnect(server.name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("巡检重连 %s 时出现未预期错误", server.name)

    async def _shutdown_sessions(self) -> None:
        """并发关闭全部会话。"""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        if not sessions:
            return
        await asyncio.gather(
            *(session.close() for session in sessions), return_exceptions=True
        )
