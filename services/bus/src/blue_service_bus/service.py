"""bus 服务生命周期：准备 RabbitMQ，再按需启动 TCP 代理。"""

from __future__ import annotations

import logging

from blue.errors import RuntimeConfigError
from blue_service_bus.config import BusConfig
from blue_service_bus.proxy import TcpProxy
from blue_service_bus.rabbitmq import RabbitMQManager

logger = logging.getLogger(__name__)

__all__ = ["BusService"]


class BusService:
    """管理 RabbitMQ 与可选代理的可启动服务。"""

    def __init__(self, config: BusConfig) -> None:
        self._config = config
        self._manager: RabbitMQManager | None = None
        self._proxy: TcpProxy | None = None

    async def start(self) -> None:
        """准备上游 RabbitMQ，再启动代理。"""
        if self._config.transport != "rabbit":
            raise RuntimeConfigError("bus 服务当前只支持 rabbit 传输")
        manager = RabbitMQManager(self._config)
        await manager.start()
        self._manager = manager
        if self._config.proxy:
            proxy = TcpProxy(self._config)
            await proxy.start()
            self._proxy = proxy
        logger.info("bus 服务已就绪：%s", self._config.url)

    async def stop(self) -> None:
        """先停代理，再停本服务启动的 RabbitMQ。"""
        proxy, self._proxy = self._proxy, None
        if proxy is not None:
            await proxy.stop()
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.stop()
