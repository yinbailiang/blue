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

from blue.idempotency import SqliteDedupeRecorder
from blue.transport import BusRuntime


class _BrokenRuntime(BusRuntime):
    """队列打不开的运行时（模拟 broker 没起来）。"""

    def __init__(
        self,
        *,
        member_id: str,
        events: Iterable[type[EventDeclaration]],
        idempotency: SqliteDedupeRecorder,
    ) -> None:
        super().__init__(member_id=member_id, events=events, idempotency=idempotency)

    async def _open_queue(self) -> EventQueue:
        """模拟连接失败。"""
        raise RuntimeError("broker 没起来")

    async def _close_queue(self) -> None:
        """没有队列要关。"""


def test_start_failure_closes_dedupe_recorder(tmp_path: Path) -> None:
    """``start()`` 失败后去重连接必须已关闭（用公开行为断言，不摸私有字段）。"""

    async def run() -> None:
        recorder = SqliteDedupeRecorder(tmp_path / "dedupe.sqlite")
        runtime = _BrokenRuntime(member_id="m", events=[], idempotency=recorder)

        with pytest.raises(RuntimeError, match="broker"):
            await runtime.start()

        with pytest.raises(RuntimeError, match="start"):
            await recorder.is_processed("m", "evt-1")

    asyncio.run(run())
