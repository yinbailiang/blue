"""WebUI 服务生命周期：FastAPI + uvicorn 与总线后端。"""

from __future__ import annotations

import asyncio
import contextlib

import uvicorn

from blue.transport import BusRuntime
from blue_service_webui.app import create_app
from blue_service_webui.backend import BusBackend
from blue_service_webui.config import WebUIConfig

__all__ = ["WebUIService"]


class _UvicornServer(uvicorn.Server):
    """不接管 SIGINT/SIGTERM；由 blue.lifecycle 统一处理进程信号。"""

    def install_signal_handlers(self) -> None:
        """跳过 uvicorn 的信号注册。"""
        return


class WebUIService:
    """托管 FastAPI 应用与总线成员的生命周期服务。"""

    def __init__(self, *, config: WebUIConfig, runtime: BusRuntime) -> None:
        self._config = config
        self._backend = BusBackend(
            runtime,
            request_timeout=config.request_timeout,
            state_dir=config.resolve_state_dir(),
        )
        self._app = create_app(backend=self._backend, config=config)
        self._server: _UvicornServer | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def backend(self) -> BusBackend:
        """总线后端（测试与诊断用）。"""
        return self._backend

    async def start(self) -> None:
        """启动总线成员，再启动 HTTP 服务。"""
        await self._backend.start()
        task: asyncio.Task[None] | None = None
        server: _UvicornServer | None = None
        try:
            server = _UvicornServer(
                uvicorn.Config(
                    self._app,
                    host=self._config.host,
                    port=self._config.port,
                    log_level="info",
                    access_log=False,
                    lifespan="off",
                )
            )
            task = asyncio.create_task(server.serve(), name="webui.uvicorn")
            deadline = asyncio.get_running_loop().time() + self._config.startup_timeout
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("WebUI HTTP 服务未能启动")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("WebUI HTTP 服务启动超时")
                await asyncio.sleep(0.05)
        except BaseException:
            if server is not None:
                server.should_exit = True
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._backend.stop()
            raise
        self._server = server
        self._task = task

    async def stop(self) -> None:
        """优雅停止 HTTP 服务，再断开总线。"""
        server, task = self._server, self._task
        self._server = None
        self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._backend.stop()
