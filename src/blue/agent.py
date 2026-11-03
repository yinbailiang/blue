"""Agent 核心：中心化维护会话，在总线上完成「模型 → 工具 → 模型」循环。

职责边界：

- **会话中心** —— 会话状态（消息历史、锁、更新时间）只存在于 Agent 进程；
- **模型调用** —— 用 ``infinity-agent`` 的流式客户端与 :class:`ToolRegistry`
  产出的工具定义完成 function calling；
- **工具调用** —— 通过总线 RPC 把 ``ToolCall`` 转发给对应工具进程，进程可分布在
  任意机器上。
"""

from __future__ import annotations

import json
import logging

from infinity_agent import Message, StreamChunk, ToolCall, ToolDefinition
from infinity_agent.clients import (
    DeepSeekConfig,
    DeepSeekRequestConfig,
    LLMClient,
    OpenAIConfig,
    OpenAIRequestConfig,
    create_client,
)
from infinity_bus import EventBus
from infinity_bus.templates.request import request

from blue.catalog import ToolCatalogClient
from blue.config import AgentSettings, ConfigError
from blue.protocol import (
    SessionPromptRequest,
    SessionReplyResponse,
    SessionResetRequest,
    SessionResetResponse,
    SessionStateRequest,
    SessionStateResponse,
    ToolCallResult,
    Topic,
    TopicBook,
)
from blue.session import Session, SessionStore
from blue.subscribers.session import SessionHandler
from blue.transport import BusRuntime

logger = logging.getLogger(__name__)

__all__ = ["AgentCore", "build_client"]


def build_client(settings: AgentSettings) -> LLMClient:
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
        settings: AgentSettings,
        client: LLMClient | None = None,
        sessions: SessionStore | None = None,
        catalog: ToolCatalogClient | None = None,
        owns_runtime: bool = True,
    ) -> None:
        """构造 Agent 核心。

        :param runtime: 总线运行时。
        :param settings: 模型与会话参数。
        :param client: 可注入的 LLM 客户端（测试用）；缺省按配置创建。
        :param sessions: 可注入的会话仓库；缺省按 ``system_prompt`` 创建。
        :param catalog: 可注入的工具目录客户端；缺省随运行时创建。
        :param owns_runtime: 是否由本对象负责关闭总线（聚合运行时传 ``False``）。
        """
        self._runtime = runtime
        self._topics: TopicBook = runtime.topics
        self._settings = settings
        self._client = client
        self._client_entered = False
        self._owns_runtime = owns_runtime
        self._sessions = sessions or SessionStore(system_prompt=settings.system_prompt)
        self._catalog: ToolCatalogClient = catalog or ToolCatalogClient(
            runtime=runtime, topics=runtime.topics
        )

    @property
    def sessions(self) -> SessionStore:
        """会话仓库。"""
        return self._sessions

    @property
    def catalog(self) -> ToolCatalogClient:
        """工具目录缓存。"""
        return self._catalog

    @property
    def topics(self) -> TopicBook:
        """主题表。"""
        return self._topics

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
        """处理一条用户消息：串行执行该会话的完整推理-调用循环。"""
        self._sessions.prune()
        session = self._sessions.get(payload.session_id)
        async with session.lock:
            try:
                text, steps = await self._run_turn(session, payload.text, bus_proxy)
            except Exception as error:
                logger.exception("会话 %s 处理失败", session.session_id)
                session.touch()
                return SessionReplyResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    success=False,
                    error_msg=str(error),
                    steps=session.steps,
                )
        return SessionReplyResponse(
            session_id=payload.session_id,
            request_id=payload.request_id,
            text=text,
            steps=steps,
        )

    def handle_reset(self, payload: SessionResetRequest) -> SessionResetResponse:
        """清空指定会话的历史。"""
        self._sessions.reset(payload.session_id)
        return SessionResetResponse(
            session_id=payload.session_id, request_id=payload.request_id
        )

    def handle_state(self, payload: SessionStateRequest) -> SessionStateResponse:
        """返回全部会话的元信息。"""
        return SessionStateResponse(
            session_id=payload.session_id,
            request_id=payload.request_id,
            sessions=self._sessions.snapshot(),
        )

    async def _run_turn(
        self, session: Session, text: str, bus_proxy: EventBus.Proxy
    ) -> tuple[str, int]:
        """执行一轮对话：模型可多次调用工具，直到给出最终答复或达到步数上限。"""
        session.messages.append(Message.user(text))
        reply = ""
        steps = 0
        while steps < self._settings.max_steps:
            steps += 1
            tools = await self._tools_for(bus_proxy)
            chunks: list[StreamChunk] = [
                chunk
                async for chunk in self._require_client().stream_chat(
                    session.messages,
                    self._request_config(tools),
                )
            ]
            assistant = self._require_client().chunks_to_message(chunks)
            session.messages.append(assistant)
            if isinstance(assistant.content, str) and assistant.content.strip():
                reply = assistant.content.strip()
            if not assistant.tool_calls:
                break
            for tool_call in assistant.tool_calls:
                content = await self._invoke_tool(
                    bus_proxy, session.session_id, tool_call
                )
                session.messages.append(
                    Message.tool(content=content, tool_call_id=tool_call.id)
                )
        else:
            notice = f"（已达到最大步数 {self._settings.max_steps}，任务可能未完成）"
            reply = f"{reply}\n\n{notice}".strip() if reply else notice
        session.steps = steps
        session.touch()
        return reply, steps

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

    async def _invoke_tool(
        self, bus_proxy: EventBus.Proxy, session_id: str, tool_call: ToolCall
    ) -> str:
        """通过总线调用一个工具进程，任何失败都转成工具错误文本回灌模型。"""
        name = tool_call.function.name
        try:
            response = await request(
                bus_proxy,
                self._topics.name(Topic.TOOL_CALL),
                {"tool": name, "arguments": tool_call.function.arguments},
                self._topics.name(Topic.TOOL_RESULT),
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
