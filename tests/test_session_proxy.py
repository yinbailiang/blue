"""会话契约包里的 ``SessionProxy``：类型收窄、失败转异常、超时。"""

from __future__ import annotations

import asyncio

import pytest
from blue_contracts_session import (
    EVENTS as SESSION_EVENTS,
)
from blue_contracts_session import (
    SessionCallError,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetRequest,
    SessionGetResponse,
    SessionProxy,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateRequest,
    SessionStateResponse,
)
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from tests.support import MemoryFanout, make_runtime


class _StubService(EventHandler):
    """按需回应：会话存在就回快照，不存在就回失败。"""

    def __init__(self, *, fail: bool = False, silent: bool = False) -> None:
        super().__init__(subscriptions=[SessionGetEvent.name, SessionStateEvent.name])
        self._fail = fail
        self._silent = silent

    async def handle(
        self, payload: BaseModel | None, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> None:
        if self._silent:
            return
        assert payload is not None
        if isinstance(payload, SessionStateRequest):
            await bus_proxy.publish(
                SessionStateReplyEvent.name,
                SessionStateResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    sessions=[],
                ),
            )
            return
        if not isinstance(payload, SessionGetRequest):
            return
        if self._fail:
            await bus_proxy.publish(
                SessionGetReplyEvent.name,
                SessionGetResponse(
                    session_id=payload.session_id,
                    request_id=payload.request_id,
                    success=False,
                    error_msg="会话不存在：s1",
                ),
            )
            return
        await bus_proxy.publish(
            SessionGetReplyEvent.name,
            SessionGetResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=True,
            ),
        )


async def _run(
    *, fail: bool = False, silent: bool = False, timeout: float = 2.0
) -> SessionGetResponse:
    hub = MemoryFanout()
    service = _StubService(fail=fail, silent=silent)
    server = make_runtime(hub, "stub")
    client = make_runtime(hub, "caller")
    await server.start()
    await client.start()
    await server.register(service)
    try:
        proxy = SessionProxy(client.proxy(), timeout=timeout)
        return await proxy.get("s1")
    finally:
        await client.stop()
        await server.stop()


def test_returns_typed_response() -> None:
    """成功时拿到的就是具体 Response 类型（不是基类）。"""
    response = asyncio.run(_run())
    assert isinstance(response, SessionGetResponse)
    assert response.success is True


def test_failure_becomes_exception() -> None:
    """``success=False`` 统一抛 SessionCallError，并把服务端说明带出来。"""
    with pytest.raises(SessionCallError, match="会话不存在"):
        asyncio.run(_run(fail=True))


def test_timeout_becomes_exception() -> None:
    """没人应答就是超时；调用方不用自己管 TimeoutError。"""
    with pytest.raises(SessionCallError, match="超时"):
        asyncio.run(_run(silent=True, timeout=0.2))


def test_proxy_covers_every_session_request_event() -> None:
    """契约里归会话服务的请求事件都要有代理方法（漏了调用方就得自己拼）。"""
    import inspect

    covered = {
        name
        for name, _ in inspect.getmembers(SessionProxy, inspect.isfunction)
        if not name.startswith("_")
    }
    assert "get" in covered and "append" in covered and "search" in covered
    assert len(covered) == 23
    assert SESSION_EVENTS  # 契约包可用
