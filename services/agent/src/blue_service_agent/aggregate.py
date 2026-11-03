"""Agent 聚合：模型核心 + 目录缓存的生命周期装配。

工具注册中心已拆为独立 ``registry`` 服务；本聚合只负责在同一总线成员上
启停 Agent 核心，控制面不导入它。
"""

from __future__ import annotations

import logging

from infinity_agent.clients import LLMClient

from blue.transport import BusRuntime
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_agent.session_gateway import SessionGateway

logger = logging.getLogger(__name__)

__all__ = ["AgentAggregate"]


class AgentAggregate:
    """Agent 本体聚合：模型核心及其总线生命周期。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        agent: AgentConfig,
        client: LLMClient | None = None,
        session_gateway: SessionGateway | None = None,
    ) -> None:
        """构造聚合。

        :param runtime: 共享的总线运行时（由聚合负责启停）。
        :param agent: Agent 模型与会话参数。
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

    @property
    def agent(self) -> AgentCore:
        """Agent 核心。"""
        return self._agent

    @property
    def is_running(self) -> bool:
        """聚合是否已启动。"""
        return self._runtime.is_running

    async def start(self) -> None:
        """启动总线与 Agent 核心。"""
        await self._runtime.start()
        try:
            await self._agent.start()
        except Exception:
            await self.stop()
            raise
        logger.info("本体聚合已启动（%s）", self._runtime.member_id)

    async def stop(self) -> None:
        """停止 Agent 核心与总线。"""
        try:
            await self._agent.stop()
        finally:
            await self._runtime.stop()
