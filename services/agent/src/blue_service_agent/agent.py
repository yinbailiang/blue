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
import time
from typing import Any

from blue_contracts_session import (
    SessionActivity,
    SessionActivityEvent,
    SessionActivityPayload,
    SessionAttachmentBlob,
    SessionPromptRequest,
    SessionReplyResponse,
    SessionStreamEvent,
    SessionStreamPayload,
    StreamToolCall,
)
from blue_contracts_tool import (
    ToolAttachment,
    ToolCallEvent,
    ToolCallResult,
    ToolResultEvent,
)
from infinity_agent import (
    Message,
    MultiModalContent,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)
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
from infinity_agent.clients.open_ai.config import OpenAIConnectionConfig
from infinity_bus import EventBus
from infinity_bus.templates.request import request

from blue.errors import ConfigError
from blue.messages import decode_messages
from blue.transport import BusRuntime
from blue_service_agent.attachments import (
    AttachmentMaterializer,
    build_user_message,
    tool_attachment_parts,
)
from blue_service_agent.catalog import ToolCatalogClient
from blue_service_agent.compact import compact_history, estimate_chars, learn_profiles
from blue_service_agent.config import AgentConfig
from blue_service_agent.history import (
    interrupted_reply,
    interrupted_step_messages,
    sanitize_history,
)
from blue_service_agent.session_gateway import BusSessionGateway, SessionGateway
from blue_service_agent.subscribers.session import SessionHandler

logger = logging.getLogger(__name__)

__all__ = ["AgentCore", "build_client"]


def _connection_config(settings: AgentConfig) -> OpenAIConnectionConfig:
    """把 Agent 的超时/重试配置映射成客户端连接配置。

    库的默认值是读超时 60s、连接 10s、HTTP 重试 3 次——60s 对长回答和大上下文
    太紧，所以由 blue 统一控制，并通过 ``blue.infd`` 暴露出来。
    """
    return OpenAIConnectionConfig(
        timeout=settings.llm_timeout,
        connect_timeout=settings.llm_connect_timeout,
        max_retries=settings.llm_http_retries,
    )


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
                connection=_connection_config(settings),
            )
        )
    return create_client(
        OpenAIConfig(
            model=settings.model,
            api_key=api_key,
            base_url=settings.base_url or "https://api.openai.com/v1",
            connection=_connection_config(settings),
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
        self._activity: dict[str, SessionActivity] = {}
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
        settings = self._settings
        logger.info(
            "Agent 核心已启动（%s）：provider=%s model=%s 读超时=%ss 连接超时=%ss "
            "流重试=%s HTTP重试=%s",
            self._runtime.member_id,
            settings.provider,
            settings.model,
            settings.llm_timeout,
            settings.llm_connect_timeout,
            settings.llm_retries,
            settings.llm_http_retries,
        )

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
        await self._set_activity(
            bus_proxy,
            payload.session_id,
            running=True,
            phase="thinking",
            request_id=payload.request_id,
        )
        try:
            return await self._run_prompt(payload, bus_proxy)
        finally:
            if self._active_turns.get(payload.session_id) is task:
                self._active_turns.pop(payload.session_id, None)
            await self._set_activity(
                bus_proxy,
                payload.session_id,
                running=False,
                request_id=payload.request_id,
            )

    def activity(self) -> list[SessionActivity]:
        """当前正在运行的会话（前端刷新后靠它恢复追踪）。"""
        return [item for item in self._activity.values() if item.running]

    async def _set_activity(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        *,
        running: bool,
        phase: str = "",
        step: int = 0,
        request_id: str = "",
    ) -> None:
        """更新并广播会话运行状态；状态没变化就不重复发。

        广播失败只记 debug：状态通知不能反过来打断正在跑的对话。
        """
        now = time.time()
        previous = self._activity.get(session_id)
        if (
            previous is not None
            and previous.running == running
            and previous.phase == phase
            and previous.step == step
            and previous.request_id == request_id
        ):
            return
        if running and (previous is None or not previous.running):
            started_at = now
        elif previous is not None:
            started_at = previous.started_at
        else:
            started_at = now
        activity = SessionActivity(
            session_id=session_id,
            running=running,
            phase=phase,
            step=step,
            request_id=request_id,
            started_at=started_at,
            updated_at=now,
        )
        if running:
            self._activity[session_id] = activity
        else:
            self._activity.pop(session_id, None)
        try:
            await bus_proxy.publish(
                SessionActivityEvent.name, SessionActivityPayload(activity=activity)
            )
        except Exception:
            logger.debug("会话运行状态广播失败：%s", session_id, exc_info=True)

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
                if self._settings.compact_history:
                    before = estimate_chars(messages)
                    messages = compact_history(
                        messages,
                        keep_messages=self._settings.compact_keep_messages,
                        text_chars=self._settings.compact_text_chars,
                        error_chars=self._settings.compact_error_chars,
                        rules=self._settings.compact_rules,
                        profiles=learn_profiles(messages),
                    )
                    after = estimate_chars(messages)
                    if before > after:
                        logger.info(
                            "历史压缩：%s 条消息，%s → %s 字符（−%s%%）",
                            len(messages),
                            before,
                            after,
                            round((1 - after / before) * 100),
                        )
                user_message, attachment_blobs = build_user_message(
                    payload.text, payload.attachments
                )
                messages.append(user_message)
                # 附件与消息同一事务落库：内容寻址天然去重
                await self._sessions.append(
                    bus_proxy, payload.session_id, [user_message], attachment_blobs
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
        materializer = AttachmentMaterializer(
            fetch=lambda digest: self._fetch_attachment(bus_proxy, digest)
        )
        while steps < self._settings.max_steps:
            steps += 1
            await self._set_activity(
                bus_proxy,
                session_id,
                running=True,
                phase="thinking",
                step=steps,
                request_id=request_id,
            )
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
            # 库里存的是附件伪 URL，模型只接受 data URL：发请求前换一份副本
            request_messages = await materializer.materialize(messages)
            chunks = await self._stream_step(
                bus_proxy, session_id, request_id, steps, request_messages, tools
            )
            assistant = self._require_client().chunks_to_message(chunks)
            messages.append(assistant)
            if isinstance(assistant.content, str) and assistant.content.strip():
                reply = assistant.content.strip()
            if not assistant.tool_calls:
                await self._sessions.append(bus_proxy, session_id, [assistant])
                break
            tool_messages: list[Message] = []
            tool_blobs: list[SessionAttachmentBlob] = []
            try:
                await self._run_tools(
                    bus_proxy,
                    session_id,
                    request_id,
                    steps,
                    assistant,
                    messages,
                    tools,
                    tool_messages,
                    tool_blobs,
                )
            except asyncio.CancelledError:
                await self._commit_interrupted_step(
                    bus_proxy, session_id, assistant, tool_messages
                )
                raise
            # assistant 与其全部工具结果一次提交：
            # 中断不会卡在「有 tool_calls 没结果」的中间态
            await self._sessions.append(
                bus_proxy, session_id, [assistant, *tool_messages], tool_blobs
            )
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

    async def _stream_step(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        request_id: str,
        step: int,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> list[StreamChunk]:
        """跑一步模型流式生成，网络中断时能救就救。

        流式读超时（aiohttp ``SocketTimeoutError``）在长回答、大上下文下很常见：

        - **一点内容都还没收到**：重放请求是安全的（没向浏览器推过任何增量），
          按 ``llm_retries`` 重试；
        - **已经收到内容**：不重试（重放会让同一段文字出现两次），但把已流出的
          那部分落库，用户至少看得到已有的回答，而不是一片空白 + 报错。
        """
        attempts = 0
        while True:
            chunks: list[StreamChunk] = []
            try:
                async for chunk in self._require_client().stream_chat(
                    messages,
                    self._request_config(tools),
                ):
                    chunks.append(chunk)
                    await self._forward_chunk(
                        bus_proxy, session_id, request_id, step, chunk
                    )
                return chunks
            except asyncio.CancelledError:
                await self._commit_interrupted_stream(bus_proxy, session_id, chunks)
                raise
            except Exception as error:
                if chunks or attempts >= self._settings.llm_retries:
                    if chunks:
                        await self._commit_interrupted_stream(
                            bus_proxy, session_id, chunks
                        )
                        logger.warning(
                            "模型流在第 %s 步中断，已落库流出的 %s 个增量：%s",
                            step,
                            len(chunks),
                            error,
                        )
                    raise
                attempts += 1
                logger.warning(
                    "模型流在第 %s 步中断（尚未收到内容），第 %s 次重试：%s",
                    step,
                    attempts,
                    error,
                )
                await asyncio.sleep(min(2.0, 0.5 * attempts))

    async def _run_tools(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        request_id: str,
        step: int,
        assistant: Message,
        messages: list[Message],
        tools: list[ToolDefinition],
        tool_messages: list[Message],
        tool_blobs: list[SessionAttachmentBlob],
    ) -> None:
        """按序执行 assistant 消息里的工具调用，结果累积到 ``tool_messages``。

        结果不即时落库：与 assistant 消息一起提交，中断时不会留下没有结果的
        ``tool_call``。``tool_messages`` 由调用方持有，中断后仍能拿到已完成的部分。
        """
        for tool_call in assistant.tool_calls or []:
            stream_call = StreamToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=tool_call.function.arguments,
            )
            await self._set_activity(
                bus_proxy,
                session_id,
                running=True,
                phase="tool",
                step=step,
                request_id=request_id,
            )
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="tool_start",
                    step=step,
                    tool_call=stream_call,
                ),
            )
            content, tool_attachments = await self._invoke_tool(
                bus_proxy, session_id, tool_call, tools
            )
            parts, blobs = tool_attachment_parts(tool_attachments)
            tool_blobs.extend(blobs)
            await self._stream_event(
                bus_proxy,
                SessionStreamPayload(
                    session_id=session_id,
                    request_id=request_id,
                    kind="tool_result",
                    step=step,
                    text=content,
                    tool_call=stream_call,
                    success=True,
                    attachments=[blob.hash for blob in blobs],
                ),
            )
            if parts:
                if content.strip():
                    parts.insert(0, MultiModalContent.text_part(content))
                tool_message = Message.tool(content=parts, tool_call_id=tool_call.id)
            else:
                tool_message = Message.tool(content=content, tool_call_id=tool_call.id)
            messages.append(tool_message)
            tool_messages.append(tool_message)

    async def _commit_interrupted_stream(
        self, bus_proxy: EventBus.Proxy, session_id: str, chunks: list[StreamChunk]
    ) -> None:
        """生成阶段被中断：落库已经流出的内容；什么都没流出就一条都不写。"""
        message = interrupted_reply(chunks)
        if message is not None:
            await self._sessions.append(bus_proxy, session_id, [message])

    async def _commit_interrupted_step(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        assistant: Message,
        tool_messages: list[Message],
    ) -> None:
        """工具执行阶段被中断：只落库已完成的调用与结果。"""
        messages = interrupted_step_messages(assistant, tool_messages)
        if messages:
            await self._sessions.append(bus_proxy, session_id, messages)

    async def _forward_chunk(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        request_id: str,
        step: int,
        chunk: StreamChunk,
    ) -> None:
        """把模型流式 chunk 转成 WebUI 可消费的事件。"""
        if (isinstance(chunk, TextChunk) and chunk.text) or (
            isinstance(chunk, ThinkingChunk) and chunk.text
        ):
            await self._set_activity(
                bus_proxy,
                session_id,
                running=True,
                phase="streaming",
                step=step,
                request_id=request_id,
            )
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

    async def _fetch_attachment(
        self, bus_proxy: EventBus.Proxy, digest: str
    ) -> str | None:
        """按哈希取附件并拼成 data URL；失败返回 None 让调用方降级。"""
        try:
            payload = await self._sessions.get_attachment(bus_proxy, digest)
        except Exception:
            logger.warning("附件 %s 读取失败", digest, exc_info=True)
            return None
        if payload is None:
            logger.warning("附件 %s 不存在（可能已被回收）", digest)
            return None
        return payload.data_url()

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
    ) -> tuple[str, list[ToolAttachment]]:
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
            return (
                _error(
                    f"工具 {name} 不存在（可用工具：{'、'.join(sorted(available))}），"
                    "请改用可用工具或直接回答"
                ),
                [],
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
            return (
                _error(f"工具 {name} 调用超时（{self._settings.tool_timeout}s）"),
                [],
            )
        except Exception as error:
            return _error(f"工具 {name} 调用失败：{error}"), []
        if isinstance(response, ToolCallResult):
            if response.success:
                return response.content, list(response.attachments)
            return (
                _error(response.error_msg or response.content or "工具执行失败"),
                [],
            )
        return _error(f"工具 {name} 返回了非预期的响应类型"), []

    def _require_client(self) -> LLMClient:
        """取已进入上下文的模型客户端。"""
        client = self._client
        if client is None:
            raise ConfigError("LLM 客户端尚未初始化")
        return client


def _error(message: str) -> str:
    """把错误包装成工具消息内容（JSON，便于模型理解）。"""
    return json.dumps({"error": message}, ensure_ascii=False)
