"""Agent 核心：在总线上完成「模型 → 工具 → 模型」循环。

职责边界：

- **会话访问** —— 通过总线 RPC 调用独立 session 服务读取/追加消息；
- **模型调用** —— 用 ``infinity-agent`` 的流式客户端完成 function calling，
  并通过 ``session.stream`` 广播实时增量；
- **工具调用** —— 通过总线 RPC 把 ``ToolCall`` 转发给对应工具进程，进程可分布在
  任意机器上。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from infinity_agent import Message, StreamChunk, ToolCall, ToolDefinition
from infinity_agent.clients import (
    DeepSeekConfig,
    DeepSeekRequestConfig,
    LLMClient,
    OpenAIConfig,
    OpenAIRequestConfig,
    TextChunk,
    ThinkingChunk,
    ToolCallCompleteChunk,
    create_client,
)
from infinity_bus import EventBus
from infinity_bus.templates.request import request

from blue.errors import ConfigError
from blue.messages import decode_messages
from blue.protocol import (
    SessionPromptRequest,
    SessionReplyResponse,
    SessionStreamEvent,
    SessionStreamPayload,
    StreamToolCall,
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from blue.transport import BusRuntime
from blue_service_agent.catalog import ToolCatalogClient
from blue_service_agent.config import AgentConfig
from blue_service_agent.history import sanitize_history
from blue_service_agent.session_gateway import BusSessionGateway, SessionGateway
from blue_service_agent.subscribers.session import SessionHandler

logger = logging.getLogger(__name__)

__all__ = ["AgentCore", "build_client"]


def build_client(settings: AgentConfig) -> LLMClient:
    """按配置创建 LLM 客户端。

    :raises ConfigError: 凭证环境变量缺失。
    """
    api_key = settings.resolve_api_key()
    if settings.provider == "deepseek":
        return create_client(
            DeepSeekConfig(
                model=settings.model,
                api_key=api_key,
                base_url=settings.base_url or "https://api.deepseek.com",
            )
        )
    return create_client(
        OpenAIConfig(
            model=settings.model,
            api_key=api_key,
            base_url=settings.base_url or "https://api.openai.com/v1",
        )
    )


class AgentCore:
    """Agent 核心服务：处理会话消息，驱动模型与总线上的工具协作。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        settings: AgentConfig,
        client: LLMClient | None = None,
        session_gateway: SessionGateway | None = None,
        catalog: ToolCatalogClient | None = None,
        owns_runtime: bool = True,
    ) -> None:
        """构造 Agent 核心。

        :param runtime: 总线运行时。
        :param settings: 模型与会话参数。
        :param client: 可注入的 LLM 客户端（测试用）；缺省按配置创建。
        :param session_gateway: 可注入的会话网关；缺省通过总线访问 session 服务。
        :param catalog: 可注入的工具目录客户端；缺省随运行时创建。
        :param owns_runtime: 是否由本对象负责关闭总线（聚合运行时传 ``False``）。
        """
        self._runtime = runtime
        self._settings = settings
        self._client = client
        self._client_entered = False
        self._owns_runtime = owns_runtime
        self._sessions: SessionGateway = session_gateway or BusSessionGateway(
            timeout=settings.session_timeout
        )
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_turns: dict[str, asyncio.Task[Any]] = {}
        self._cancelling: set[str] = set()
        self._catalog: ToolCatalogClient = catalog or ToolCatalogClient(runtime=runtime)

    @property
    def sessions(self) -> SessionGateway:
        """会话网关。"""
        return self._sessions

    @property
    def catalog(self) -> ToolCatalogClient:
        """工具目录缓存。"""
        return self._catalog

    @property
    def is_running(self) -> bool:
        """服务是否已启动。"""
        return self._runtime.is_running

    async def start(self) -> None:
        """启动总线、订阅工具目录、进入模型客户端上下文并注册会话处理器。"""
        await self._runtime.start()
        try:
            await self._catalog.start()
            if self._client is None:
                self._client = build_client(self._settings)
            await self._client.__aenter__()
            self._client_entered = True
            await self._runtime.register(SessionHandler(self))
        except Exception:
            await self.stop()
            raise
        logger.info("Agent 核心已启动（%s）", self._runtime.member_id)

    async def stop(self) -> None:
        """退出模型客户端上下文；仅在拥有总线时一并关闭总线。"""
        client, self._client = self._client, None
        if client is not None and self._client_entered:
            self._client_entered = False
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                logger.warning("关闭 LLM 客户端失败", exc_info=True)
        if self._owns_runtime:
            await self._runtime.stop()

    async def handle_prompt(
        self, payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
    ) -> SessionReplyResponse:
        """处理一条用户消息，并把这次 turn 登记为可被 :meth:`cancel` 中断。"""
        task = asyncio.current_task()
        if task is not None:
            self._active_turns[payload.session_id] = task
        try:
            return await self._run_prompt(payload, bus_proxy)
        finally:
            if self._active_turns.get(payload.session_id) is task:
                self._active_turns.pop(payload.session_id, None)

    def cancel(self, session_id: str) -> bool:
        """请求中断某个会话正在进行的回复。

        取消是协作式的：真正停下发生在下一个 ``await`` 点。

        :return: 是否确实存在进行中的 turn。
        """
        task = self._active_turns.get(session_id)
        if task is None or task.done():
            return False
        self._cancelling.add(session_id)
        task.cancel()
        logger.info("已请求中断会话 %s 的回复", session_id)
        return True

    async def _run_prompt(
        self, payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
    ) -> SessionReplyResponse:
        """串行执行该会话的完整推理-调用循环。"""
        lock = self._session_locks.setdefault(payload.session_id, asyncio.Lock())
        steps = 0
        async with lock:
            try:
                snapshot = await self._sessions.ensure(
                    bus_proxy, payload.session_id, self._settings.system_prompt
                )
                steps = snapshot.steps
                # 历史可能带着进程中断遗留的悬空 tool_calls，
                # 直接回灌会被模型接口以 400 拒收
                messages = sanitize_history(decode_messages(snapshot.messages))
                user_message = Message.user(payload.text)
                messages.append(user_message)
                await self._sessions.append(
                    bus_proxy, payload.session_id, [user_message]
                )
                text, steps = await self._run_turn(
                    payload.session_id, payload.request_id, messages, bus_proxy
                )
                try:
                    await self._sessions.update(bus_proxy, payload.session_id, steps)
                except Exception:
                    logger.warning(
                        "会话 %s 步数更新失败", payload.session_id, exc_info=True
                    )
            except asyncio.CancelledError:
                # 只有显式 cancel 才吞掉取消，进程关闭等系统级取消继续上抛
                if payload.session_id not in self._cancelling:
                    raise
                self._cancelling.discard(payload.session_id)
                logger.info("会话 %s 的回复已被中断", payload.session_id)
                await self._stream_event(
                    bus_proxy,
                    SessionStreamPayload(
                        session_id=payload.session_id,
                        request_id=payload.request_id,
                        kind="cancelled",
                        step=steps,
                        text="已中断",
                    ),
                )
                return SessionReplyResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    success=False,
                    error_msg="已中断",
                    steps=steps,
                )
            except Exception as error:
                logger.exception("会话 %s 处理失败", payload.session_id)
                await self._stream_event(
                    bus_proxy,
                    SessionStreamPayload(
                        session_id=payload.session_id,
                        request_id=payload.request_id,
                        kind="error",
                        step=steps,
                        text=str(error),
                    ),
                )
                return SessionReplyResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    success=False,
                    error_msg=str(error),
                    steps=steps,
                )
        return SessionReplyResponse(
            session_id=payload.session_id,
            request_id=payload.request_id,
            text=text,
            steps=steps,
        )

    async def _run_turn(
        self,
        session_id: str,
        request_id: str,
        messages: list[Message],
        bus_proxy: EventBus.Proxy,
    ) -> tuple[str, int]:
        """执行一轮对话：模型可多次调用工具，直到最终答复或达到步数上限。"""
        reply = ""
        steps = 0
        while steps < self._settings.max_steps:
            steps += 1
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="step_start",
                    step=steps,
                ),
            )
            tools = await self._tools_for(bus_proxy)
            chunks: list[StreamChunk] = []
            async for chunk in self._require_client().stream_chat(
                messages,
                self._request_config(tools),
            ):
                chunks.append(chunk)
                await self._forward_chunk(
                    bus_proxy, session_id, request_id, steps, chunk
                )
            assistant = self._require_client().chunks_to_message(chunks)
            messages.append(assistant)
            await self._sessions.append(bus_proxy, session_id, [assistant])
            if isinstance(assistant.content, str) and assistant.content.strip():
                reply = assistant.content.strip()
            if not assistant.tool_calls:
                break
            for tool_call in assistant.tool_calls:
                stream_call = StreamToolCall(
                    id=tool_call.id,
                    name=tool_call.function.name,
                    arguments=tool_call.function.arguments,
                )
                await self._stream_event(
                    bus_proxy,
                    SessionStreamPayload(
                        session_id=session_id,
                        request_id=request_id,
                        kind="tool_start",
                        step=steps,
                        tool_call=stream_call,
                    ),
                )
                content = await self._invoke_tool(
                    bus_proxy, session_id, tool_call, tools
                )
                await self._stream_event(
                    bus_proxy,
                    SessionStreamPayload(
                        session_id=session_id,
                        request_id=request_id,
                        kind="tool_result",
                        step=steps,
                        text=content,
                        tool_call=stream_call,
                        success=True,
                    ),
                )
                tool_message = Message.tool(content=content, tool_call_id=tool_call.id)
                messages.append(tool_message)
                await self._sessions.append(bus_proxy, session_id, [tool_message])
        else:
            notice = f"（已达到最大步数 {self._settings.max_steps}，任务可能未完成）"
            reply = f"{reply}\n\n{notice}".strip() if reply else notice
        await self._stream_event(
            bus_proxy,
            SessionStreamPayload(
                session_id=session_id,
                request_id=request_id,
                kind="done",
                step=steps,
                text=reply,
            ),
        )
        return reply, steps

    async def _forward_chunk(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        request_id: str,
        step: int,
        chunk: StreamChunk,
    ) -> None:
        """把模型流式 chunk 转成 WebUI 可消费的事件。"""
        if isinstance(chunk, TextChunk) and chunk.text:
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="text",
                    step=step,
                    text=chunk.text,
                ),
            )
        elif isinstance(chunk, ThinkingChunk) and chunk.text:
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="thinking",
                    step=step,
                    text=chunk.text,
                ),
            )
        elif isinstance(chunk, ToolCallCompleteChunk) and chunk.tool_calls:
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="tool_calls",
                    step=step,
                    tool_calls=[
                        StreamToolCall(
                            id=call.id,
                            name=call.function.name,
                            arguments=call.function.arguments,
                        )
                        for call in chunk.tool_calls
                    ],
                ),
            )

    async def _stream_event(
        self, bus_proxy: EventBus.Proxy, payload: SessionStreamPayload
    ) -> None:
        """发布流式活动；失败只记录 debug，不打断主流程。"""
        try:
            await bus_proxy.publish(SessionStreamEvent.name, payload)
        except Exception:
            logger.debug("流式事件发布失败", exc_info=True)

    def _request_config(self, tools: list[ToolDefinition]) -> OpenAIRequestConfig:
        """按 provider 构造请求级配置（DeepSeek 客户端要求其专属类型）。"""
        if self._settings.provider == "deepseek":
            return DeepSeekRequestConfig(tools=tools or None)
        return OpenAIRequestConfig(tools=tools or None)

    async def _tools_for(self, bus_proxy: EventBus.Proxy) -> list[ToolDefinition]:
        """取工具目录：缓存为空时向注册中心拉取一次。"""
        tools = self._catalog.tools
        if tools:
            return tools
        return await self._catalog.refresh(bus_proxy)

    async def _available_tool_names(
        self, bus_proxy: EventBus.Proxy, tools: list[ToolDefinition], name: str
    ) -> set[str]:
        """可调用工具名集合；``name`` 已在缓存中时不再拉取目录。

        目录为空说明注册中心尚未就绪，此时返回空集合，调用方据此跳过存在性
        拦截，避免把所有工具误判为不存在。
        """
        known = {tool.function.name for tool in tools}
        if not known or name in known:
            return known
        refreshed = await self._catalog.refresh(bus_proxy)
        return {tool.function.name for tool in refreshed}

    async def _invoke_tool(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        tool_call: ToolCall,
        tools: list[ToolDefinition],
    ) -> str:
        """通过总线调用一个工具进程，任何失败都转成工具错误文本回灌模型。"""
        name = tool_call.function.name
        available = await self._available_tool_names(bus_proxy, tools, name)
        if available and name not in available:
            # 不存在的工具没有任何进程会应答，照常发起只会一直等到 tool_timeout
            logger.warning(
                "模型请求了不存在的工具 %s，已直接回灌错误（可用：%s）",
                name,
                "、".join(sorted(available)),
            )
            return _error(
                f"工具 {name} 不存在（可用工具：{'、'.join(sorted(available))}），"
                "请改用可用工具或直接回答"
            )
        try:
            response = await request(
                bus_proxy,
                ToolCallEvent.name,
                {"tool": name, "arguments": tool_call.function.arguments},
                ToolResultEvent.name,
                session_id=session_id,
                timeout=self._settings.tool_timeout,
            )
        except TimeoutError:
            return _error(f"工具 {name} 调用超时（{self._settings.tool_timeout}s）")
        except Exception as error:
            return _error(f"工具 {name} 调用失败：{error}")
        if isinstance(response, ToolCallResult):
            if response.success:
                return response.content
            return _error(response.error_msg or response.content or "工具执行失败")
        return _error(f"工具 {name} 返回了非预期的响应类型")

    def _require_client(self) -> LLMClient:
        """取已进入上下文的模型客户端。"""
        client = self._client
        if client is None:
            raise ConfigError("LLM 客户端尚未初始化")
        return client


def _error(message: str) -> str:
    """把错误包装成工具消息内容（JSON，便于模型理解）。"""
    return json.dumps({"error": message}, ensure_ascii=False)
