"""本体聚合：把会话中心与工具目录作为同一个运行实体启动。

聚合只管自己：一个进程、一个总线成员，托管全部核心能力。

- 会话中心（:class:`~blue.agent.AgentCore`）：模型循环、工具调用编排、中心会话；
- 工具目录（:class:`~blue.catalog.ToolRegistryService`）：工具租约、注册、目录广播。

外围程序（工具进程、用户界面）**不在聚合内**，它们作为独立进程运行并通过总线
与聚合通信；聚合不管理它们的生命周期，也不提供启动它们的入口。
"""

from __future__ import annotations

import logging

from infinity_agent.clients import LLMClient

from blue.agent import AgentCore
from blue.catalog import ToolRegistryService
from blue.config import BlueSettings
from blue.protocol import TopicBook
from blue.session import SessionStore
from blue.transport import BusRuntime

logger = logging.getLogger(__name__)

__all__ = ["AgentAggregate"]


class AgentAggregate:
    """Agent 本体聚合：同一总线成员上的会话中心 + 工具目录。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        settings: BlueSettings,
        client: LLMClient | None = None,
        sessions: SessionStore | None = None,
    ) -> None:
        """构造聚合。

        :param runtime: 共享的总线运行时（由聚合负责启停）。
        :param settings: 完整配置。
        :param client: 可注入的 LLM 客户端（测试用）。
        :param sessions: 可注入的会话仓库（测试用）。
        """
        self._runtime = runtime
        self._settings = settings
        self._agent = AgentCore(
            runtime=runtime,
            settings=settings.agent,
            client=client,
            sessions=sessions,
            owns_runtime=False,
        )
        self._registry = ToolRegistryService(
            runtime=runtime,
            ttl=settings.registry.ttl,
            sweep_interval=settings.registry.sweep_interval,
            owns_runtime=False,
        )

    @property
    def agent(self) -> AgentCore:
        """会话中心。"""
        return self._agent

    @property
    def registry(self) -> ToolRegistryService:
        """工具目录。"""
        return self._registry

    @property
    def topics(self) -> TopicBook:
        """主题表。"""
        return self._runtime.topics

    @property
    def is_running(self) -> bool:
        """聚合是否已启动。"""
        return self._runtime.is_running

    async def start(self) -> None:
        """启动总线，再依次激活工具目录与会话中心。"""
        await self._runtime.start()
        try:
            await self._registry.start()
            await self._agent.start()
        except Exception:
            await self.stop()
            raise
        logger.info("本体聚合已启动（%s）", self._runtime.member_id)

    async def stop(self) -> None:
        """按启动的逆序停止：会话中心 → 工具目录 → 总线。"""
        try:
            await self._agent.stop()
        finally:
            try:
                await self._registry.stop()
            finally:
                await self._runtime.stop()
