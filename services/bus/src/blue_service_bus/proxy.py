"""可选的 AMQP TCP 代理：监听本地端口并转发到上游 RabbitMQ。"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from blue_service_bus.config import BusConfig
from blue_service_bus.rabbitmq import parse_amqp_url

logger = logging.getLogger(__name__)

__all__ = ["TcpProxy"]


class TcpProxy:
    """轻量 TCP 转发器。"""

    def __init__(self, config: BusConfig) -> None:
        self._listen = parse_amqp_url(config.proxy_listen)
        self._upstream = parse_amqp_url(config.url)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """开始监听。"""
        self._server = await asyncio.start_server(
            self._handle, self._listen.host, self._listen.port
        )
        logger.info(
            "AMQP 代理已启动：%s:%s -> %s:%s",
            self._listen.host,
            self._listen.port,
            self._upstream.host,
            self._upstream.port,
        )

    async def stop(self) -> None:
        """停止监听并关闭所有连接。"""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self._upstream.host, self._upstream.port
            )
        except OSError:
            writer.close()
            await writer.wait_closed()
            return

        async def pump(
            source: asyncio.StreamReader, target: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    data = await source.read(65536)
                    if not data:
                        break
                    target.write(data)
                    await target.drain()
            except ConnectionError, OSError:
                return

        tasks = [
            asyncio.create_task(pump(reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for target in (writer, upstream_writer):
                target.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await target.wait_closed()
