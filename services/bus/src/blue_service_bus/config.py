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
    password: str | None = None
    configure: bool = True
    queue_max_bytes: int = Field(default=1_073_741_824, ge=0)
    """单条 fanout 队列的体积上限（字节），0 = 不限。

    超限时 RabbitMQ 按 ``drop-head`` 丢最旧的消息（不阻塞发布方）——这是磁盘的
    最后一道闸：真正该拦住孤儿队列的是上面的 ``queue_expires_ms``，这里只兜底
    「有消费者但不读」的卡死场景。
    """

    queue_expires_ms: int = Field(default=1_800_000, ge=0)
    """fanout 队列的空闲过期时间（毫秒），0 = 不过期。

    成员队列是 durable 且非 auto-delete（要能承载 restart 策略的补投积压），
    进程被 kill 时不会有人解绑 —— 没有这层兜底，孤儿队列会一直绑在 fanout 上
    接收每一份广播，磁盘无限增长（实测 172 个孤儿队列攒了 5600 万条、49 GB）。
    """
    proxy: bool = False
    proxy_listen: str = "amqp://127.0.0.1:5673/blue"
    startup_timeout: float = Field(default=60.0, gt=0)
    stop_timeout: float = Field(default=30.0, gt=0)
