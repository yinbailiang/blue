"""工具运行时注册中心：租约、目录版本与目录广播。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from blue_contracts_tool import (
    ToolDefinition,
    ToolProxy,
    ToolRegisterPayload,
)
from infinity_bus import EventBus

from blue.transport import BusRuntime
from blue_service_registry.subscribers.registry import RegistryHandler

logger = logging.getLogger(__name__)

__all__ = ["ToolRegistryService", "ToolRegistryState", "WorkerRecord"]


@dataclass(frozen=True)
class WorkerRecord:
    """一个工具进程的登记记录。"""

    worker_id: str
    tool: ToolDefinition
    expires_at: float

    @property
    def tool_name(self) -> str:
        """工具名。"""
        return self.tool.function.name


class ToolRegistryState:
    """工具登记表：租约 + 版本号（不依赖总线，便于单测）。"""

    def __init__(self) -> None:
        self._records: dict[str, WorkerRecord] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """目录版本号：工具集合每变化一次递增。"""
        return self._version

    @property
    def worker_count(self) -> int:
        """在册工具进程数。"""
        return len(self._records)

    def tools(self) -> list[ToolDefinition]:
        """当前可用工具定义（按工具名排序）。"""
        return [
            record.tool
            for record in sorted(
                self._records.values(), key=lambda item: item.tool_name
            )
        ]

    def record(self, worker_id: str) -> WorkerRecord | None:
        """查询单个工具进程记录。"""
        return self._records.get(worker_id)

    def register(
        self,
        worker_id: str,
        tool: ToolDefinition,
        ttl: float,
        *,
        now: float | None = None,
    ) -> bool:
        """登记或刷新一个工具进程。

        同名工具以最新登记的进程为准（旧的同名进程记录被移除）。

        :return: 目录（版本）是否发生变化。
        """
        timestamp = time.time() if now is None else now
        tool_name = tool.function.name
        existing = self._records.get(worker_id)
        replaced = [
            other_id
            for other_id, other in self._records.items()
            if other_id != worker_id and other.tool_name == tool_name
        ]
        for other_id in replaced:
            del self._records[other_id]
            logger.warning("工具 %r 由新进程接管，移除旧进程 %s", tool_name, other_id)

        self._records[worker_id] = WorkerRecord(
            worker_id=worker_id, tool=tool, expires_at=timestamp + ttl
        )
        changed = bool(replaced) or existing is None or existing.tool != tool
        if changed:
            self._version += 1
        return changed

    def heartbeat(
        self, worker_id: str, ttl: float, *, now: float | None = None
    ) -> bool:
        """续约；进程未登记时返回 ``False``（调用方应要求其重新注册）。"""
        record = self._records.get(worker_id)
        if record is None:
            return False
        timestamp = time.time() if now is None else now
        self._records[worker_id] = WorkerRecord(
            worker_id=worker_id, tool=record.tool, expires_at=timestamp + ttl
        )
        return True

    def unregister(self, worker_id: str) -> bool:
        """主动下线；返回目录是否发生变化。"""
        if self._records.pop(worker_id, None) is None:
            return False
        self._version += 1
        return True

    def sweep(self, *, now: float | None = None) -> list[str]:
        """回收租约过期的进程，返回被移除的进程 ID。"""
        timestamp = time.time() if now is None else now
        expired = [
            worker_id
            for worker_id, record in self._records.items()
            if record.expires_at <= timestamp
        ]
        for worker_id in expired:
            del self._records[worker_id]
        if expired:
            self._version += 1
        return expired


class ToolRegistryService:
    """工具注册中心进程：维护租约、应答目录查询、广播目录变更。"""

    def __init__(
        self,
        *,
        runtime: BusRuntime,
        ttl: float,
        sweep_interval: float,
        now: Callable[[], float] = time.time,
        owns_runtime: bool = True,
    ) -> None:
        self._runtime = runtime
        self._ttl = ttl
        self._sweep_interval = sweep_interval
        self._now = now
        self._owns_runtime = owns_runtime
        self._state = ToolRegistryState()
        self._sweeper: asyncio.Task[None] | None = None

    @property
    def state(self) -> ToolRegistryState:
        """登记表（只读用途）。"""
        return self._state

    @property
    def ttl(self) -> float:
        """租约时长（秒）。"""
        return self._ttl

    @property
    def clock(self) -> Callable[[], float]:
        """时钟函数（测试可注入）。"""
        return self._now

    async def start(self) -> None:
        """启动总线、注册处理器并开始租约巡检。"""
        await self._runtime.start()
        try:
            await self._runtime.register(RegistryHandler(self))
        except Exception:
            if self._owns_runtime:
                await self._runtime.stop()
            raise
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="registry.sweeper")
        logger.info("工具注册中心已启动（%s）", self._runtime.member_id)

    async def stop(self) -> None:
        """停止巡检；仅在拥有总线时一并关闭总线。"""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
        if self._owns_runtime:
            await self._runtime.stop()

    async def broadcast_catalog(self, proxy: EventBus.Proxy | None = None) -> None:
        """广播当前工具目录（失败只记日志，不影响登记）。"""
        version = self._state.version
        try:
            active = proxy or self._runtime.proxy()
            await ToolProxy(active).publish_catalog(version, self._state.tools())
        except Exception:
            logger.warning("广播工具目录失败（version=%s）", version, exc_info=True)

    async def _sweep_loop(self) -> None:
        """周期性回收过期租约。"""
        while True:
            await asyncio.sleep(self._sweep_interval)
            expired = self._state.sweep(now=self._now())
            if expired:
                logger.info("工具租约过期，已移除：%s", ", ".join(expired))
                await self.broadcast_catalog()

    def register_tool(self, payload: ToolRegisterPayload) -> bool:
        """登记工具进程，返回目录是否变化。"""
        return self._state.register(
            payload.worker_id, payload.tool, payload.ttl, now=self._now()
        )

    def heartbeat_tool(self, worker_id: str) -> bool:
        """刷新租约，返回目标是否仍在册。"""
        return self._state.heartbeat(worker_id, ttl=self._ttl, now=self._now())

    def unregister_tool(self, worker_id: str) -> bool:
        """移除工具进程，返回目录是否变化。"""
        return self._state.unregister(worker_id)

    def catalog_snapshot(self) -> tuple[int, list[ToolDefinition]]:
        """返回 ``(版本号, 工具定义列表)``。"""
        return self._state.version, self._state.tools()
