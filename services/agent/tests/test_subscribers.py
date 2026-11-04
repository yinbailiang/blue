"""核心订阅者测试：订阅声明、事件翻译与超时策略。"""

from __future__ import annotations

import asyncio
from typing import cast

from blue_contracts_session import (
    SessionActivity,
    SessionActivityQueryEvent,
    SessionCancelEvent,
    SessionPromptEvent,
    SessionPromptRequest,
    SessionReplyResponse,
)
from blue_contracts_tool import (
    CatalogChangedEvent,
    CatalogChangedPayload,
    CatalogQueryEvent,
    ToolCallEvent,
    ToolCallRequest,
    ToolHeartbeatEvent,
    ToolHeartbeatPayload,
    ToolRegisterEvent,
    ToolRegisterPayload,
    ToolUnregisterEvent,
)
from blue_service_agent.subscribers.catalog import CatalogChangedHandler
from blue_service_agent.subscribers.session import SessionHandler
from blue_service_registry import RegistryHandler
from infinity_agent import ToolDefinition
from infinity_bus import Event, EventBus
from pydantic import BaseModel

from blue.subscribers.tool_call import ToolCallHandler


class RecordingProxy:
    """记录发布事件的假代理。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel | None]] = []

    async def publish(self, name: str, data: BaseModel | None = None) -> None:
        """记录一次发布。"""
        self.published.append((name, data))


class FakeRegistryBackend:
    """只记录状态变更的注册中心替身。"""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.heartbeats: list[str] = []
        self.unregistered: list[str] = []
        self.broadcasts = 0
        self.snapshot: tuple[int, list[ToolDefinition]] = (7, [])

    def register_tool(self, payload: ToolRegisterPayload) -> bool:
        self.registered.append(payload.worker_id)
        return True

    def heartbeat_tool(self, worker_id: str) -> bool:
        self.heartbeats.append(worker_id)
        return False

    def unregister_tool(self, worker_id: str) -> bool:
        self.unregistered.append(worker_id)
        return True

    def catalog_snapshot(self) -> tuple[int, list[ToolDefinition]]:
        return self.snapshot

    async def broadcast_catalog(self, proxy: EventBus.Proxy | None = None) -> None:
        del proxy
        self.broadcasts += 1


def test_registry_handler_subscribes_registry_events() -> None:
    """注册中心订阅者订阅四类固定事件。"""
    handler = RegistryHandler(FakeRegistryBackend())

    assert set(handler.subscriptions.patterns) == {
        ToolRegisterEvent.name,
        ToolHeartbeatEvent.name,
        ToolUnregisterEvent.name,
        CatalogQueryEvent.name,
    }


def test_registry_handler_translates_events() -> None:
    """事件被翻译为后端调用，并在目录变化时广播。"""
    backend = FakeRegistryBackend()
    handler = RegistryHandler(backend)
    proxy = cast(EventBus.Proxy, RecordingProxy())
    tool = ToolDefinition.create("echo", "回显")

    async def scenario() -> None:
        register = ToolRegisterPayload(
            session_id="", request_id="req-1", worker_id="w1", tool=tool, ttl=5.0
        )
        await handler.handle(
            register,
            proxy,
            Event(name=ToolRegisterEvent.name, data=register),
        )
        heartbeat = ToolHeartbeatPayload(
            session_id="", request_id="req-2", worker_id="w1"
        )
        await handler.handle(
            heartbeat,
            proxy,
            Event(name=ToolHeartbeatEvent.name, data=heartbeat),
        )

    asyncio.run(scenario())

    assert backend.registered == ["w1"]
    assert backend.heartbeats == ["w1"]
    assert backend.broadcasts == 1


def test_catalog_handler_applies_snapshot() -> None:
    """目录广播被写入缓存。"""
    applied: list[tuple[int, list[ToolDefinition]]] = []

    class FakeCatalog:
        def apply(self, version: int, tools: list[ToolDefinition]) -> bool:
            applied.append((version, tools))
            return True

    handler = CatalogChangedHandler(FakeCatalog())
    payload = CatalogChangedPayload(
        version=3, tools=[ToolDefinition.create("echo", "")]
    )

    asyncio.run(
        handler.handle(
            payload,
            cast(EventBus.Proxy, RecordingProxy()),
            Event(name=CatalogChangedEvent.name, data=payload),
        )
    )

    assert applied[0][0] == 3
    assert applied[0][1][0].function.name == "echo"


def test_session_handler_has_no_timeout() -> None:
    """会话订阅者不能被总线默认超时打断。"""

    class FakeSessions:
        async def handle_prompt(
            self, payload: SessionPromptRequest, bus_proxy: EventBus.Proxy
        ) -> SessionReplyResponse:
            del bus_proxy
            return SessionReplyResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                text=payload.text,
            )

        def cancel(self, session_id: str) -> bool:
            del session_id
            return False

        def activity(self) -> list[SessionActivity]:
            return []

    handler = SessionHandler(FakeSessions())

    assert handler.handle_timeout is None
    assert list(handler.subscriptions.patterns) == [
        SessionPromptEvent.name,
        SessionCancelEvent.name,
        SessionActivityQueryEvent.name,
    ]
    assert len(handler.subscriptions) == 3


def test_tool_call_handler_subscribes_tool_call() -> None:
    """工具进程只订阅 tool.call。"""

    class FakeWorker:
        async def handle_call(
            self, payload: ToolCallRequest, bus_proxy: EventBus.Proxy
        ) -> None:
            del payload, bus_proxy

    handler = ToolCallHandler(FakeWorker())

    assert list(handler.subscriptions.patterns) == [ToolCallEvent.name]
    assert handler.handle_timeout is None
