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

登记与续约的逻辑收敛在 :class:`ToolRegistrar`：它按「租约」工作，
一个进程可以持有任意条租约，因此一个进程承载多个工具（如 MCP 桥）无需
复制心跳代码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from blue_contracts_tool import (
    ToolAttachment,
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
from blue.tool_context import ToolContext, use_context
from blue.transport import BusRuntime

logger = logging.getLogger(__name__)

__all__ = ["ToolLease", "ToolRegistrar", "ToolWorker", "unpack_tool_output"]


@dataclass(frozen=True)
class ToolLease:
    """一条工具租约：承载者标识 + 它对外提供的工具定义。

    租约以 ``worker_id`` 为键，因此同一工具由新进程接管时，注册中心会移除旧记录。
    一个进程可以持有任意条租约。
    """

    worker_id: str
    definition: ToolDefinition

    @property
    def tool_name(self) -> str:
        """本租约提供的工具名。"""
        return self.definition.function.name


class ToolRegistrar:
    """把一组工具租约登记到注册中心，并周期性补发注册与续约。

    只负责「让目录看到这些工具」，不关心工具如何执行；:class:`ToolWorker`
    与多工具进程（如 MCP 桥）共用同一套登记语义。
    """

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        leases: Iterable[ToolLease],
        ttl: float = 15.0,
    ) -> None:
        """构造登记器。

        :param runtime: 已装配好的总线运行时（须先 :meth:`~BusRuntime.start`）。
        :param leases: 要登记的租约集合。
        :param ttl: 租约时长（秒），心跳间隔为其三分之一。
        """
        if ttl <= 0:
            raise ValueError("ttl 必须 > 0")
        self._runtime = runtime
        self._leases: tuple[ToolLease, ...] = tuple(leases)
        for lease in self._leases:
            if not lease.worker_id.strip():
                raise ValueError("worker_id 不能为空")
        self._ttl = ttl
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._withdrawn = False

    @property
    def leases(self) -> tuple[ToolLease, ...]:
        """本登记器负责的全部租约。"""
        return self._leases

    @property
    def ttl(self) -> float:
        """租约时长（秒）。"""
        return self._ttl

    async def announce(self) -> None:
        """登记全部租约并启动续约循环（重复调用幂等）。"""
        self._withdrawn = False
        await self._publish_register()
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="tool-registrar.heartbeat"
            )

    async def withdraw(self) -> None:
        """停止续约并注销全部租约（幂等，重复调用不再发事件）。"""
        if self._withdrawn:
            return
        self._withdrawn = True
        heartbeat, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if not self._runtime.is_running:
            return
        proxy = self._runtime.proxy()
        for lease in self._leases:
            with suppress(Exception):
                await proxy.publish(
                    ToolUnregisterEvent.name,
                    ToolUnregisterPayload(worker_id=lease.worker_id),
                )

    async def _publish_register(self) -> None:
        """逐条登记租约。"""
        proxy = self._runtime.proxy()
        for lease in self._leases:
            await proxy.publish(
                ToolRegisterEvent.name,
                ToolRegisterPayload(
                    worker_id=lease.worker_id,
                    tool=lease.definition,
                    ttl=self._ttl,
                ),
            )

    async def _heartbeat_loop(self) -> None:
        """周期性补发注册并续约；单次失败不影响后续。"""
        interval = max(self._ttl / 3.0, 0.1)
        while True:
            await asyncio.sleep(interval)
            try:
                proxy = self._runtime.proxy()
                for lease in self._leases:
                    await proxy.publish(
                        ToolRegisterEvent.name,
                        ToolRegisterPayload(
                            worker_id=lease.worker_id,
                            tool=lease.definition,
                            ttl=self._ttl,
                        ),
                    )
                    await proxy.publish(
                        ToolHeartbeatEvent.name,
                        ToolHeartbeatPayload(worker_id=lease.worker_id),
                    )
            except Exception:
                logger.debug(
                    "心跳发送失败：%s",
                    ", ".join(lease.worker_id for lease in self._leases),
                    exc_info=True,
                )


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
        self._registrar = ToolRegistrar(
            runtime=runtime,
            leases=[ToolLease(worker_id=self._worker_id, definition=self._definition)],
            ttl=ttl,
        )

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

    @property
    def registrar(self) -> ToolRegistrar:
        """承载本工具的登记器。"""
        return self._registrar

    async def start(self) -> None:
        """启动总线、注册调用处理器并登记到注册中心。"""
        await self._runtime.start()
        try:
            await self._runtime.register(ToolCallHandler(self))
            await self._registrar.announce()
        except Exception:
            await self._runtime.stop()
            raise
        logger.info("工具进程已上线：%s（%s）", self.tool_name, self._worker_id)

    async def stop(self) -> None:
        """下线：停止心跳 → 注销 → 关闭总线。"""
        await self._registrar.withdraw()
        await self._runtime.stop()
        logger.info("工具进程已下线：%s", self.tool_name)

    async def handle_call(
        self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """处理一次工具调用（只响应自己的工具名）。"""
        if payload.tool != self.tool_name:
            return
        # 工具函数拿不到自己的 schema 之外的东西（那些字段是要发给模型看的），
        # 所以「为哪个会话/哪次请求干活」通过 ContextVar 传进去。
        with use_context(
            ToolContext(
                session_id=payload.session_id,
                request_id=payload.request_id,
                tool=payload.tool,
            )
        ):
            message = await self._registry.invoke(
                ToolCall(
                    id=payload.request_id,
                    function=FunctionCall(
                        name=payload.tool, arguments=payload.arguments
                    ),
                )
            )
        text, ok, attachments = unpack_tool_output(message.content)
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
                attachments=attachments,
            ),
        )


def unpack_tool_output(
    content: MessageContent | None,
) -> tuple[str, bool, list[ToolAttachment]]:
    """把工具执行结果转成（文本, 是否成功, 附件）。

    ``ToolRegistry`` 对 ``-> str`` 返回 JSON 字符串、对模型返回 JSON 对象，
    对异常返回 ``{"error": ...}``；这里统一降维为可读文本。

    工具可以把自己的 ``attachments`` 字段放进返回对象（见
    :func:`blue.runtime.tool_attachment`）：该字段会被摘出来放进
    ``ToolCallResult.attachments``，正文里不再出现 base64。
    """
    raw = content if isinstance(content, str) else ""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return raw, True, []
    if isinstance(decoded, str):
        return decoded, True, []
    if isinstance(decoded, dict):
        mapping = cast(dict[str, object], decoded)
        error = mapping.get("error")
        if isinstance(error, str):
            return error, False, []
        attachments = _extract_attachments(mapping)
        if attachments:
            mapping = {
                key: value for key, value in mapping.items() if key != "attachments"
            }
        text = json.dumps(mapping, ensure_ascii=False, indent=2)
        return text, True, attachments
    return json.dumps(decoded, ensure_ascii=False, indent=2), True, []


def _extract_attachments(mapping: dict[str, object]) -> list[ToolAttachment]:
    """从工具返回对象里提取 ``attachments``（非法条目跳过并告警）。"""
    raw = mapping.get("attachments")
    if not isinstance(raw, list):
        return []
    extracted: list[ToolAttachment] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            logger.warning("工具返回了非对象附件，已跳过")
            continue
        entry = cast(dict[str, object], item)
        mime = entry.get("mime")
        data = entry.get("data")
        if not isinstance(mime, str) or not isinstance(data, str):
            logger.warning("工具返回的附件缺少 mime/data，已跳过")
            continue
        name = entry.get("name")
        extracted.append(
            ToolAttachment(
                mime=mime, data=data, name=name if isinstance(name, str) else None
            )
        )
    return extracted
