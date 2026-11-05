"""进程生命周期：把「可启动 / 可停止」的服务跑起来并等待退出信号。

核心与外置程序共用：服务实现提供 ``start`` / ``stop``，工具进程用
``blue_tool_sdk.ToolWorker`` 作为服务。
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from typing import Protocol

__all__ = ["LifecycleService", "serve_forever"]


class LifecycleService(Protocol):
    """可被 ``serve_forever`` 托管的服务。"""

    async def start(self) -> None:
        """启动服务。"""
        ...

    async def stop(self) -> None:
        """停止服务并释放资源。"""
        ...


async def serve_forever(service: LifecycleService) -> None:
    """启动服务，收到 SIGINT / SIGTERM 后优雅停止。"""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await service.start()
    try:
        await stop.wait()
    finally:
        await service.stop()
