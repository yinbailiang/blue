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
    ToolHeartbeatReplyEvent,
    ToolHeartbeatResponse,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolRegisterReplyEvent,
    ToolRegisterResponse,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterPayload,
    ToolUnregisterReplyEvent,
    ToolUnregisterResponse,
)
from infinity_agent import ToolDefinition, ToolFunction, ToolParameters
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import RequestProtocol, request
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

    def __init__(self, *, lease_alive: bool = True) -> None:
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
        self._lease_alive = lease_alive

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        """按载荷类型归档，并像真注册中心那样回一句结果。

        登记/续约/下线现在是**请求/应答**：不回话的话 worker 会等超时。
        """
        if not isinstance(payload, RequestProtocol):
            return
        sid, rid = payload.session_id, payload.request_id
        if isinstance(payload, ToolRegisterPayload):
            self.registers.append(payload)
            await bus_proxy.publish(
                ToolRegisterReplyEvent.name,
                ToolRegisterResponse(
                    session_id=sid, request_id=rid, changed=True, version=1
                ),
            )
        elif isinstance(payload, ToolHeartbeatPayload):
            self.heartbeats.append(payload)
            await bus_proxy.publish(
                ToolHeartbeatReplyEvent.name,
                ToolHeartbeatResponse(
                    session_id=sid, request_id=rid, registered=self._lease_alive
                ),
            )
        elif isinstance(payload, ToolUnregisterPayload):
            self.unregisters.append(payload)
            await bus_proxy.publish(
                ToolUnregisterReplyEvent.name,
                ToolUnregisterResponse(
                    session_id=sid, request_id=rid, removed=True, changed=True
                ),
            )


def _leases(*names: str) -> list[ToolLease]:
    """按工具名造租约，worker_id 用确定性写法。"""
    return [
        ToolLease(worker_id=f"mcp.srv.{name}", definition=_definition(name))
        for name in names
    ]


_Body = Callable[[MemoryRuntime, _Recorder], Awaitable[None]]


async def _with_recorder(
    hub: MemoryFanout, body: _Body, *, lease_alive: bool = True
) -> None:
    """建一个客户端成员并挂上记录器，交给 ``body`` 使用。"""
    runtime = make_runtime(hub, "recorder")
    recorder = _Recorder(lease_alive=lease_alive)
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


def test_registrar_heartbeat_reannounces_when_lease_is_lost() -> None:
    """心跳发现租约失效（注册中心重启/过期）时补一次登记。"""
    asyncio.run(_heartbeat_reannounces(lease_alive=False))


def test_registrar_heartbeat_only_renews_when_lease_is_alive() -> None:
    """租约还在时只续约、不重复登记（少一半广播）。"""
    asyncio.run(_heartbeat_reannounces(lease_alive=True))


async def _heartbeat_reannounces(*, lease_alive: bool) -> None:
    hub = MemoryFanout()
    runtime = make_runtime(hub, "bridge")
    registrar = ToolRegistrar(runtime=runtime, leases=_leases("a", "b"), ttl=0.3)

    async def body(_runtime: MemoryRuntime, recorder: _Recorder) -> None:
        await runtime.start()
        try:
            await registrar.announce()
            await wait_until(lambda: len(recorder.heartbeats) >= 2, timeout=3.0)
            if lease_alive:
                # 只续约：登记只有 announce 那一次（两条租约）
                await wait_until(lambda: len(recorder.registers) >= 2, timeout=3.0)
                assert len(recorder.registers) == 2
            else:
                # 租约失效 → 每个心跳周期补登记一次
                await wait_until(lambda: len(recorder.registers) >= 4, timeout=3.0)
        finally:
            await registrar.withdraw()
            await runtime.stop()

    await _with_recorder(hub, body, lease_alive=lease_alive)


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


def test_worker_extracts_attachments_from_tool_output() -> None:
    """工具返回对象里的 attachments 被摘出来，正文里不留 base64。"""
    asyncio.run(_worker_extracts_attachments())


async def _worker_extracts_attachments() -> None:
    from blue.runtime import tool_attachment

    def screenshot() -> dict[str, object]:
        """返回一张「截图」。"""
        return {
            "text": "截图如下",
            "attachments": [tool_attachment(b"png-bytes", "image/png")],
        }

    hub = MemoryFanout()
    worker = ToolWorker(
        runtime=make_runtime(hub, "tool.screenshot"), tool=screenshot, ttl=5.0
    )
    client = make_runtime(hub, "client")
    await worker.start()
    await client.start()
    try:
        result = await request(
            client.proxy(),
            ToolCallEvent.name,
            {"tool": "screenshot", "arguments": {}},
            ToolResultEvent.name,
            timeout=5.0,
        )
        assert isinstance(result, ToolCallResult)
        assert result.success is True
        assert "cG5nLWJ5dGVz" not in result.content  # base64 不进正文
        assert [item.mime for item in result.attachments] == ["image/png"]
        assert result.attachments[0].data == "cG5nLWJ5dGVz"
        assert "截图如下" in result.content
    finally:
        await client.stop()
        await worker.stop()


def test_unpack_skips_malformed_attachments() -> None:
    """附件条目缺 mime/data 时跳过，不影响正文。"""
    from blue.worker import unpack_tool_output

    payload = '{"text": "hi", "attachments": [{"mime": "image/png"}, "junk"]}'
    text, ok, attachments = unpack_tool_output(payload)
    assert ok is True
    assert attachments == []
    assert "hi" in text
