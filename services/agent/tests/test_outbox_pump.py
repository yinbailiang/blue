"""发件箱队列泵：顺序执行、失败重试、上限后留在队列、启动恢复。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_registry import ToolRegistryService
from infinity_agent import Message, Messages, StreamChunk
from infinity_agent.clients import RequestConfig
from tests.support import (
    MemoryRuntime,
    MemorySessionGateway,
    ScriptedClient,
    make_runtime,
    wait_until,
)

from blue.transport import MemoryFanout


class FlakyClient(ScriptedClient):
    """前 ``failures`` 次调用直接抛错，之后返回一条正常回复。"""

    def __init__(self, reply: str = "好了", failures: int = 0) -> None:
        super().__init__([Message.assistant(content=reply) for _ in range(20)])
        self.failures = failures
        self.attempts = 0

    async def stream_chat(
        self, messages: Messages, config: RequestConfig | None = None
    ) -> AsyncGenerator[StreamChunk]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("模型超时")
        async for chunk in super().stream_chat(messages, config):
            yield chunk


@dataclass
class Harness:
    """一套跑起来的 agent + 注册中心，队列网关是内存实现。"""

    core: AgentCore
    gateway: MemorySessionGateway
    client: FlakyClient
    registry: ToolRegistryService


async def _start(
    script: list[Message] | None = None,
    *,
    client: FlakyClient | None = None,
    max_attempts: int = 3,
    seed: list[str] | None = None,
) -> tuple[Harness, MemoryRuntime, MemoryFanout]:
    """起环境；``seed`` 里的文本会先放进该会话的队列。"""
    hub = MemoryFanout()
    registry = ToolRegistryService(
        runtime=make_runtime(hub, "registry"), ttl=5.0, sweep_interval=0.05
    )
    gateway = MemorySessionGateway()
    for text in seed or []:
        gateway.enqueue("s1", text)
    chosen = client or FlakyClient()
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(
            api_key="sk-test",
            max_steps=4,
            system_prompt="sys",
            outbox_max_attempts=max_attempts,
        ),
        client=chosen,
        session_gateway=gateway,
    )
    chat: MemoryRuntime = make_runtime(hub, "chat")
    await registry.start()
    await core.start()
    await chat.start()
    return (
        Harness(core=core, gateway=gateway, client=chosen, registry=registry),
        chat,
        hub,
    )


async def _stop(harness: Harness, chat: MemoryRuntime) -> None:
    await chat.stop()
    await harness.core.stop()
    await harness.registry.stop()


def test_queued_messages_run_in_order_after_a_turn() -> None:
    """一轮结束后，队列里的消息按顺序继续跑完（每轮结束都会拉起队列泵）。"""
    asyncio.run(_queued_in_order())


async def _queued_in_order() -> None:
    gateway_seed = ["排队一", "排队二"]
    harness, chat, _ = await _start(seed=gateway_seed)
    try:
        # 有个排队消息 → 通过广播拉起队列泵
        harness.core.nudge_outbox("s1", chat.proxy())
        await wait_until(lambda: not harness.gateway.queued("s1"), timeout=10.0)

        texts = [
            message.content
            for message in harness.gateway.messages["s1"]
            if message.role.value == "user"
        ]
        assert texts == gateway_seed
        assert harness.client.attempts == len(gateway_seed)
    finally:
        await _stop(harness, chat)


def test_failed_message_is_retried_then_left_for_manual_retry() -> None:
    """失败会按配置自动重试；到上限后留在队列里带错误原因，等人工。"""
    asyncio.run(_retry_then_give_up())


async def _retry_then_give_up() -> None:
    client = FlakyClient(failures=99, reply="不会成功")
    harness, chat, _ = await _start(client=client, max_attempts=2, seed=["会失败"])
    try:
        harness.core.nudge_outbox("s1", chat.proxy())
        await wait_until(
            lambda: any(
                item.state == "failed" for item in harness.gateway.queued("s1")
            ),
            timeout=10.0,
        )
        pending = harness.gateway.queued("s1")
        assert len(pending) == 1
        assert pending[0].state == "failed"
        assert pending[0].attempts == 2  # 上限 2：试满就停
        assert pending[0].error and "模型超时" in pending[0].error
        # 每次「消息级尝试」内部还会按 llm_retries 重试流，所以流的次数 ≥ 2
        assert client.attempts >= 2
    finally:
        await _stop(harness, chat)


def test_retry_recovers_after_a_transient_failure() -> None:
    """偶发失败：自动重试一次就成功，消息从队列里消失。"""
    asyncio.run(_transient_failure())


async def _transient_failure() -> None:
    client = FlakyClient(failures=1, reply="第二次成了")
    harness, chat, _ = await _start(client=client, max_attempts=3, seed=["会先失败"])
    try:
        harness.core.nudge_outbox("s1", chat.proxy())
        await wait_until(lambda: not harness.gateway.queued("s1"), timeout=15.0)
        # 每次「消息级尝试」内部还会按 llm_retries 重试流，所以流的次数 ≥ 2
        assert client.attempts >= 2
        replies = [
            message.content
            for message in harness.gateway.messages["s1"]
            if message.role.value == "assistant"
        ]
        assert replies == ["第二次成了"]
    finally:
        await _stop(harness, chat)


def test_startup_recovers_leftover_queue() -> None:
    """进程重启后自动把遗留的排队消息跑掉，不必等下一次发言。"""
    asyncio.run(_startup_recovery())


async def _startup_recovery() -> None:
    harness, chat, _ = await _start(seed=["重启前的遗留消息"])
    try:
        # _start 里 core.start() 已经扫过一遍队列
        await wait_until(lambda: not harness.gateway.queued("s1"), timeout=10.0)
        texts = [
            message.content
            for message in harness.gateway.messages["s1"]
            if message.role.value == "user"
        ]
        assert texts == ["重启前的遗留消息"]
    finally:
        await _stop(harness, chat)
