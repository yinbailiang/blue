"""工具进程：一个进程只承载一个工具。

启动流程：

1. 用 ``infinity_agent.ToolRegistry`` 从函数签名推断 :class:`ToolDefinition`；
2. 连上总线，订阅 ``tool.call``，只处理属于自己的调用；
3. 发布 ``tool.register`` 把自己登记到工具注册中心；
4. 周期性补发注册并发送 ``tool.heartbeat`` 续约，进程退出时发布
   ``tool.unregister``。

周期补发注册可以覆盖「工具先于 Agent 启动」的竞态；注册中心按 worker_id
幂等处理。

这样工具既不需要知道 Agent 在哪，也不需要被 Agent 静态导入——注册中心
把目录广播给所有 Agent，Agent 按名字发起调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any, cast

from blue_contracts_tool import (
    ToolCallRequest,
    ToolCallResult,
    ToolHeartbeatEvent,
    ToolHeartbeatPayload,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterPayload,
)
from infinity_agent import (
    FunctionCall,
    MessageContent,
    ToolCall,
    ToolDefinition,
)
from infinity_agent.tools import ToolRegistry
from infinity_bus import EventBus

from blue.subscribers.tool_call import ToolCallHandler
from blue.transport import BusRuntime

logger = logging.getLogger(__name__)

__all__ = ["ToolWorker"]


class ToolWorker:
    """把单个工具函数变成可被总线调用的服务。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        tool: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        ttl: float = 15.0,
    ) -> None:
        """构造工具进程。

        :param runtime: 已装配好的总线运行时（调用 :meth:`start` 时启动）。
        :param tool: 工具函数；必须带完整类型签名（含返回值）。
        :param name: 工具名，默认取函数名。
        :param description: 工具描述，默认取函数 docstring。
        :param ttl: 租约时长（秒），心跳间隔为其三分之一。
        """
        if ttl <= 0:
            raise ValueError("ttl 必须 > 0")
        self._runtime = runtime
        self._registry = ToolRegistry()
        self._registry.register(tool, name=name, description=description)
        definitions = self._registry.definitions
        if len(definitions) != 1:
            raise ValueError("一个工具进程只能承载一个工具")
        self._definition = definitions[0]
        self._worker_id = (
            f"tool.{self._definition.function.name}.{uuid.uuid4().hex[:8]}"
        )
        self._ttl = ttl
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def worker_id(self) -> str:
        """工具进程唯一标识。"""
        return self._worker_id

    @property
    def tool_name(self) -> str:
        """工具名。"""
        return self._definition.function.name

    @property
    def definition(self) -> ToolDefinition:
        """工具定义（可直接作为 LLM 的 tools 参数）。"""
        return self._definition

    async def start(self) -> None:
        """启动总线、注册调用处理器并登记到注册中心。"""
        await self._runtime.start()
        try:
            await self._runtime.register(ToolCallHandler(self))
            await self._publish_register()
        except Exception:
            await self._runtime.stop()
            raise
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"worker.{self.tool_name}.heartbeat"
        )
        logger.info("工具进程已上线：%s（%s）", self.tool_name, self._worker_id)

    async def stop(self) -> None:
        """下线：停止心跳 → 注销 → 关闭总线。"""
        heartbeat, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if self._runtime.is_running:
            with suppress(Exception):
                await self._runtime.proxy().publish(
                    ToolUnregisterEvent.name,
                    ToolUnregisterPayload(worker_id=self._worker_id),
                )
        await self._runtime.stop()
        logger.info("工具进程已下线：%s", self.tool_name)

    async def handle_call(
        self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """处理一次工具调用（只响应自己的工具名）。"""
        if payload.tool != self.tool_name:
            return
        message = await self._registry.invoke(
            ToolCall(
                id=payload.request_id,
                function=FunctionCall(name=payload.tool, arguments=payload.arguments),
            )
        )
        text, ok = _unpack(message.content)
        if not ok:
            logger.warning("工具 %s 执行失败：%s", self.tool_name, text)
        await bus_proxy.publish(
            ToolResultEvent.name,
            ToolCallResult(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=ok,
                error_msg=None if ok else text,
                content=text,
            ),
        )

    async def _publish_register(self) -> None:
        """向注册中心登记自己。"""
        await self._runtime.proxy().publish(
            ToolRegisterEvent.name,
            ToolRegisterPayload(
                worker_id=self._worker_id, tool=self._definition, ttl=self._ttl
            ),
        )

    async def _heartbeat_loop(self) -> None:
        """周期性补发注册并续约；单次失败不影响后续。"""
        interval = max(self._ttl / 3.0, 0.1)
        while True:
            await asyncio.sleep(interval)
            try:
                proxy = self._runtime.proxy()
                await proxy.publish(
                    ToolRegisterEvent.name,
                    ToolRegisterPayload(
                        worker_id=self._worker_id,
                        tool=self._definition,
                        ttl=self._ttl,
                    ),
                )
                await proxy.publish(
                    ToolHeartbeatEvent.name,
                    ToolHeartbeatPayload(worker_id=self._worker_id),
                )
            except Exception:
                logger.debug("心跳发送失败：%s", self._worker_id, exc_info=True)


def _unpack(content: MessageContent | None) -> tuple[str, bool]:
    """把工具执行结果转成（文本, 是否成功）。

    ``ToolRegistry`` 对 ``-> str`` 返回 JSON 字符串、对模型返回 JSON 对象，
    对异常返回 ``{"error": ...}``；这里统一降维为可读文本。
    """
    raw = content if isinstance(content, str) else ""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return raw, True
    if isinstance(decoded, str):
        return decoded, True
    if isinstance(decoded, dict):
        mapping = cast(dict[str, object], decoded)
        error = mapping.get("error")
        if isinstance(error, str):
            return error, False
    return json.dumps(decoded, ensure_ascii=False, indent=2), True
