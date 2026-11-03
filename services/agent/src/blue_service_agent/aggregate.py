"""Agent 聚合：同一总线成员上的模型核心 + 工具目录。

该聚合是 `services/agent` 的内部实现，由 Agent 服务进程启动；核心控制面
不导入它。
"""

from __future__ import annotations

import logging

from infinity_agent.clients import LLMClient

from blue.transport import BusRuntime
from blue_service_agent.agent import AgentCore
from blue_service_agent.catalog import ToolRegistryService
from blue_service_agent.config import AgentConfig, RegistryConfig
from blue_service_agent.session_gateway import SessionGateway

logger = logging.getLogger(__name__)

__all__ = ["AgentAggregate"]


class AgentAggregate:
    """Agent 本体聚合：同一总线成员上的模型核心 + 工具目录。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        agent: AgentConfig,
        registry: RegistryConfig,
        client: LLMClient | None = None,
        session_gateway: SessionGateway | None = None,
    ) -> None:
        """构造聚合。

        :param runtime: 共享的总线运行时（由聚合负责启停）。
        :param agent: Agent 模型与会话参数。
        :param registry: 工具注册中心租约参数。
        :param client: 可注入的 LLM 客户端（测试用）。
        :param session_gateway: 可注入的会话网关（测试用）。
        """
        self._runtime = runtime
        self._agent = AgentCore(
            runtime=runtime,
            settings=agent,
            client=client,
            session_gateway=session_gateway,
            owns_runtime=False,
        )
        self._registry = ToolRegistryService(
            runtime=runtime,
            ttl=registry.ttl,
            sweep_interval=registry.sweep_interval,
            owns_runtime=False,
        )

    @property
    def agent(self) -> AgentCore:
        """Agent 核心。"""
        return self._agent

    @property
    def registry(self) -> ToolRegistryService:
        """工具目录。"""
        return self._registry

    @property
    def is_running(self) -> bool:
        """聚合是否已启动。"""
        return self._runtime.is_running

    async def start(self) -> None:
        """启动总线，再依次激活工具目录与 Agent 核心。"""
        await self._runtime.start()
        try:
            await self._registry.start()
            await self._agent.start()
        except Exception:
            await self.stop()
            raise
        logger.info("本体聚合已启动（%s）", self._runtime.member_id)

    async def stop(self) -> None:
        """按启动的逆序停止：Agent 核心 → 工具目录 → 总线。"""
        try:
            await self._agent.stop()
        finally:
            try:
                await self._registry.stop()
            finally:
                await self._runtime.stop()
