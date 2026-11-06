"""总线运行时生命周期测试：启动失败时的资源收尾。

去重记录器在 ``start()`` 里先于队列打开（要建表、要清理），所以队列打不开时它会
留在打开状态。不关就会留下 ``-wal``/``-shm``，下次打开还得回放一遍。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import pytest
from infinity_bus import EventDeclaration, EventQueue

from blue.transport import BusRuntime


class _SpyRecorder:
    """记录 start/close 次数的假记录器（扮演库的 SqliteIdempotencyRecorder）。"""

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        """计数 +1。"""
        self.started += 1

    async def close(self) -> None:
        """计数 +1。"""
        self.closed += 1

    async def is_processed(self, consumer: str, event_id: str) -> bool:
        """永远返回「未处理」（本测试不关心去重语义）。"""
        return False

    async def mark_processed(self, consumer: str, event_id: str) -> None:
        """空实现。"""

    async def claim(self, consumer: str, event_id: str) -> bool:
        """永远认领成功（本测试只关心生命周期）。"""
        return True

    async def release(self, consumer: str, event_id: str) -> None:
        """空实现。"""


class _BrokenRuntime(BusRuntime):
    """队列打不开的运行时（模拟 broker 没起来）。"""

    def __init__(
        self,
        *,
        member_id: str,
        events: Iterable[type[EventDeclaration]],
        idempotency: _SpyRecorder,
    ) -> None:
        super().__init__(member_id=member_id, events=events, idempotency=idempotency)

    async def _open_queue(self) -> EventQueue:
        """模拟连接失败。"""
        raise RuntimeError("broker 没起来")

    async def _close_queue(self) -> None:
        """没有队列要关。"""


def test_start_failure_closes_dedupe_recorder(tmp_path: Path) -> None:
    """``start()`` 失败后去重连接必须已关闭（否则下次打开要回放 WAL）。"""

    async def run() -> None:
        recorder = _SpyRecorder()
        runtime = _BrokenRuntime(member_id="m", events=[], idempotency=recorder)

        with pytest.raises(RuntimeError, match="broker"):
            await runtime.start()

        assert recorder.started == 1, "记录器应当先于队列被打开"
        assert recorder.closed == 1, "启动失败也必须关掉记录器"

    asyncio.run(run())
