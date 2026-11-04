"""工具契约包里的 ``ToolProxy``：调用工具、注册中心管理（单向广播 + 目录查询）。"""

from __future__ import annotations

import asyncio

import pytest
from blue_contracts_tool import (
    CatalogQueryEvent,
    CatalogQueryRequest,
    CatalogQueryResponse,
    CatalogReplyEvent,
    ToolCallError,
    ToolCallEvent,
    ToolCallRequest,
    ToolCallResult,
    ToolHeartbeatEvent,
    ToolHeartbeatReplyEvent,
    ToolHeartbeatResponse,
    ToolProxy,
    ToolRegisterEvent,
    ToolRegisterReplyEvent,
    ToolRegisterResponse,
    ToolResultEvent,
    ToolUnregisterEvent,
    ToolUnregisterReplyEvent,
    ToolUnregisterResponse,
)
from infinity_agent import ToolDefinition, ToolFunction, ToolParameters
from infinity_bus import Event, EventBus, EventHandler
from infinity_bus.templates.request import RequestProtocol
from pydantic import BaseModel

from tests.support import MemoryFanout, make_runtime

_NOW_TOOL = ToolDefinition(
    function=ToolFunction(
        name="now",
        description="读取当前时间",
        parameters=ToolParameters(type="object", properties={}, required=[]),
    )
)


class _Recorder(EventHandler):
    """记录收到的单向事件；``tool.call`` / ``catalog.query`` 分别按需应答。"""

    def __init__(self, *, fail_call: bool = False, silent: bool = False) -> None:
        super().__init__(
            subscriptions=[
                ToolCallEvent.name,
                CatalogQueryEvent.name,
                ToolRegisterEvent.name,
                ToolHeartbeatEvent.name,
                ToolUnregisterEvent.name,
                ToolRegisterReplyEvent.name,
                ToolHeartbeatReplyEvent.name,
                ToolUnregisterReplyEvent.name,
            ]
        )
        self.seen: list[str] = []
        self._fail = fail_call
        self._silent = silent

    async def handle(
        self, payload: BaseModel | None, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> None:
        self.seen.append(raw_event.name)
        if self._silent:
            return
        if raw_event.name == CatalogQueryEvent.name and isinstance(
            payload, CatalogQueryRequest
        ):
            await bus_proxy.publish(
                CatalogReplyEvent.name,
                CatalogQueryResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    version=7,
                    tools=[],
                ),
            )
            return
        if isinstance(payload, RequestProtocol):
            sid, rid = payload.session_id, payload.request_id
        else:  # pragma: no cover - 事件都带请求载荷
            sid, rid = "", ""
        if raw_event.name == ToolRegisterEvent.name:
            await bus_proxy.publish(
                ToolRegisterReplyEvent.name,
                ToolRegisterResponse(
                    session_id=sid,
                    request_id=rid,
                    changed=True,
                    version=3,
                ),
            )
            return
        if raw_event.name == ToolHeartbeatEvent.name:
            await bus_proxy.publish(
                ToolHeartbeatReplyEvent.name,
                ToolHeartbeatResponse(
                    session_id=sid,
                    request_id=rid,
                    registered=False,
                ),
            )
            return
        if raw_event.name == ToolUnregisterEvent.name:
            await bus_proxy.publish(
                ToolUnregisterReplyEvent.name,
                ToolUnregisterResponse(
                    session_id=sid,
                    request_id=rid,
                    removed=True,
                    changed=True,
                ),
            )
            return
        if raw_event.name == ToolCallEvent.name and isinstance(
            payload, ToolCallRequest
        ):
            await bus_proxy.publish(
                ToolResultEvent.name,
                ToolCallResult(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    success=not self._fail,
                    content="ok" if not self._fail else "",
                    error_msg=None if not self._fail else "工具炸了",
                ),
            )


async def _run(
    action: str, *, fail_call: bool = False, silent: bool = False
) -> tuple[object, list[str]]:
    hub = MemoryFanout()
    recorder = _Recorder(fail_call=fail_call, silent=silent)
    server = make_runtime(hub, "tool")
    client = make_runtime(hub, "caller")
    await server.start()
    await client.start()
    await server.register(recorder)
    result: object = None
    try:
        proxy = ToolProxy(client.proxy(), timeout=0.3 if silent else 2.0)
        if action == "call":
            result = await proxy.call("now", {"zone": "utc"})
        elif action == "catalog":
            result = await proxy.catalog()
        elif action == "register":
            result = await proxy.register("worker-1", _NOW_TOOL, ttl=30.0)
        elif action == "heartbeat":
            result = await proxy.heartbeat("worker-1")
        elif action == "unregister":
            result = await proxy.unregister("worker-1")
    finally:
        # 单向广播是"发完就走"，给分发留一个调度机会再取记录
        await asyncio.sleep(0.05)
        seen = list(recorder.seen)
        await client.stop()
        await server.stop()
    return result, seen


def test_calls_a_tool() -> None:
    """调工具：拿回正文与附件列表。"""
    result, _ = asyncio.run(_run("call"))
    assert isinstance(result, ToolCallResult)
    assert result.content == "ok"


def test_tool_failure_becomes_exception() -> None:
    """工具返回 success=False 时抛异常，调用方不用自己判。"""
    with pytest.raises(ToolCallError, match="工具炸了"):
        asyncio.run(_run("call", fail_call=True))


def test_timeout_becomes_exception() -> None:
    """没人应答 = 超时，也统一成 ToolCallError。"""
    with pytest.raises(ToolCallError, match="超时"):
        asyncio.run(_run("call", silent=True))


def test_catalog_query() -> None:
    """目录查询有应答，返回强类型。"""
    response, _ = asyncio.run(_run("catalog"))
    assert isinstance(response, CatalogQueryResponse)
    assert response.version == 7


def test_registry_calls_return_typed_replies() -> None:
    """注册/心跳/下线都是请求/应答：拿得到结果，进程才知道租约还在不在。"""
    register, _ = asyncio.run(_run("register"))
    assert isinstance(register, ToolRegisterResponse)
    assert register.changed is True and register.version == 3

    heartbeat, _ = asyncio.run(_run("heartbeat"))
    assert isinstance(heartbeat, ToolHeartbeatResponse)
    assert heartbeat.registered is False  # 假注册中心回的是"已不在册"

    unregister, _ = asyncio.run(_run("unregister"))
    assert isinstance(unregister, ToolUnregisterResponse)
    assert unregister.removed is True
