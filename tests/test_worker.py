"""工具标签记测试：``ToolRegistrar`` 的多租约登记、续约与注销。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from blue_contracts_tool import (
    ToolCallEvent,
    ToolCallResult,
    ToolHeartbeatEvent,
    ToolHeartbeatPayload,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterPayload,
)
from infinity_agent import ToolDefinition, ToolFunction, ToolParameters
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import request
from pydantic import BaseModel

from blue.transport import MemoryFanout, MemoryRuntime
from blue.worker import ToolLease, ToolRegistrar, ToolWorker
from tests.support import make_runtime, wait_until


def _definition(name: str) -> ToolDefinition:
    """构造一个最小可用的工具定义。"""
    return ToolDefinition(
        function=ToolFunction(
            name=name,
            description=f"{name} 工具",
            parameters=ToolParameters(type="object"),
        )
    )


class _Recorder(EventHandler):
    """把登记 / 续约 / 注销三类事件分别收进列表。"""

    def __init__(self) -> None:
        super().__init__(
            subscriptions=[
                ToolRegisterEvent.name,
                ToolHeartbeatEvent.name,
                ToolUnregisterEvent.name,
            ],
            handle_timeout=None,
        )
        self.registers: list[ToolRegisterPayload] = []
        self.heartbeats: list[ToolHeartbeatPayload] = []
        self.unregisters: list[ToolUnregisterPayload] = []

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        """按载荷类型归档。"""
        del bus_proxy, raw_event
        if isinstance(payload, ToolRegisterPayload):
            self.registers.append(payload)
        elif isinstance(payload, ToolHeartbeatPayload):
            self.heartbeats.append(payload)
        elif isinstance(payload, ToolUnregisterPayload):
            self.unregisters.append(payload)


def _leases(*names: str) -> list[ToolLease]:
    """按工具名造租约，worker_id 用确定性写法。"""
    return [
        ToolLease(worker_id=f"mcp.srv.{name}", definition=_definition(name))
        for name in names
    ]


_Body = Callable[[MemoryRuntime, _Recorder], Awaitable[None]]


async def _with_recorder(hub: MemoryFanout, body: _Body) -> None:
    """建一个客户端成员并挂上记录器，交给 ``body`` 使用。"""
    runtime = make_runtime(hub, "recorder")
    recorder = _Recorder()
    await runtime.start()
    try:
        await runtime.register(recorder)
        await body(runtime, recorder)
    finally:
        await runtime.stop()


def test_registrar_announces_every_lease() -> None:
    """一个进程可以登记多条租约。"""
    asyncio.run(_announces_every_lease())


async def _announces_every_lease() -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=_leases("a", "b"), ttl=5.0)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        try:
            await registrar.announce()
            await wait_until(lambda: len(recorder.registers) == 2)
            assert {item.worker_id for item in recorder.registers} == {
                "mcp.srv.a",
                "mcp.srv.b",
            }
            assert {item.tool.function.name for item in recorder.registers} == {
                "a",
                "b",
            }
            assert all(item.ttl == 5.0 for item in recorder.registers)
        finally:
            await registrar.withdraw()
            await runtime.stop()

    await _with_recorder(hub, body)


def test_registrar_heartbeat_reannounces_and_renews() -> None:
    """心跳循环同时补发登记与续约，覆盖「工具先于 agent 启动」的竞态。"""
    asyncio.run(_heartbeat_reannounces())


async def _heartbeat_reannounces() -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=_leases("a", "b"), ttl=0.3)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        try:
            await registrar.announce()
            await wait_until(lambda: len(recorder.heartbeats) >= 2, timeout=3.0)
            assert len(recorder.registers) >= 4
        finally:
            await registrar.withdraw()
            await runtime.stop()

    await _with_recorder(hub, body)


def test_registrar_withdraw_unregisters_all_leases() -> None:
    """注销时逐条发 ``tool.unregister``，并停止心跳。"""
    asyncio.run(_withdraws_all())


async def _withdraws_all() -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=_leases("a"), ttl=5.0)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        await registrar.announce()
        await wait_until(lambda: len(recorder.registers) == 1)
        await registrar.withdraw()
        await wait_until(lambda: len(recorder.unregisters) == 1)
        assert recorder.unregisters[0].worker_id == "mcp.srv.a"
        await runtime.stop()

    await _with_recorder(hub, body)


def test_registrar_withdraw_is_idempotent() -> None:
    """重复注销不会抛错，也不重复发事件。"""
    asyncio.run(_withdraw_twice())


async def _withdraw_twice() -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=_leases("a"), ttl=5.0)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        await registrar.announce()
        await wait_until(lambda: len(recorder.registers) == 1)
        await registrar.withdraw()
        await registrar.withdraw()
        await runtime.stop()
        assert len(recorder.unregisters) == 1

    await _with_recorder(hub, body)


def test_registrar_rejects_invalid_input() -> None:
    """非法租约与 ttl 在构造期就失败。"""
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    with pytest.raises(ValueError):
        ToolRegistrar(runtime=runtime, leases=_leases("a"), ttl=0)
    with pytest.raises(ValueError):
        ToolRegistrar(
            runtime=runtime,
            leases=[ToolLease(worker_id="  ", definition=_definition("a"))],
            ttl=5.0,
        )


def test_registrar_without_leases_is_inert() -> None:
    """空租约集合可以安全登记与注销。"""
    asyncio.run(_inert_registrar())


async def _inert_registrar() -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=[], ttl=5.0)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        try:
            await registrar.announce()
            assert registrar.leases == ()
            await registrar.withdraw()
            assert recorder.registers == []
        finally:
            await runtime.stop()

    await _with_recorder(hub, body)


def test_tool_worker_uses_a_single_lease() -> None:
    """``ToolWorker`` 的登记行为与抽取前一致：一条随机 id 的租约。"""
    asyncio.run(_worker_single_lease())


async def _worker_single_lease() -> None:
    def echo(text: str) -> str:
        """回显。"""
        return text

    hub = MemoryFanout()
    worker = ToolWorker(runtime=make_runtime(hub, "tool.echo"), tool=echo, ttl=5.0)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        assert len(worker.registrar.leases) == 1
        assert worker.registrar.leases[0].worker_id == worker.worker_id
        assert worker.worker_id.startswith("tool.echo.")
        await worker.start()
        try:
            await wait_until(lambda: len(recorder.registers) == 1)
            assert recorder.registers[0].worker_id == worker.worker_id
        finally:
            await worker.stop()
        await wait_until(lambda: len(recorder.unregisters) == 1)

    await _with_recorder(hub, body)


def test_tool_worker_still_serves_calls_after_refactor() -> None:
    """抽取登记器后，工具调用链路保持不变。"""
    asyncio.run(_worker_serves_call())


async def _worker_serves_call() -> None:
    def shout(text: str) -> str:
        """把文本变大写。"""
        return text.upper()

    hub = MemoryFanout()
    worker = ToolWorker(runtime=make_runtime(hub, "tool.shout"), tool=shout, ttl=5.0)
    client = make_runtime(hub, "client")
    await worker.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "shout", "arguments": {"text": "hi"}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert result.content == "HI"
    finally:
        await client.stop()
        await worker.stop()
