"""bus 服务运行时配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["BusConfig"]


class BusConfig(BaseModel):
    """RabbitMQ 管理、连接与代理配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["rabbit"] = "rabbit"
    url: str = "amqp://guest:guest@127.0.0.1:5672/blue"
    mode: Literal["managed", "external"] = "managed"
    rabbitmq_bin: str = "rabbitmq-server"
    rabbitmqctl_bin: str = "rabbitmqctl"
    data_dir: Path = Path("rabbitmq")
    vhost: str = "blue"
    user: str = "guest"
    password_env: str = "RABBITMQ_PASSWORD"
    configure: bool = True
    proxy: bool = False
    proxy_listen: str = "amqp://127.0.0.1:5673/blue"
    startup_timeout: float = Field(default=60.0, gt=0)
    stop_timeout: float = Field(default=30.0, gt=0)
