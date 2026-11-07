"""就绪探针：进程存活、TCP 可连接，以及从 AMQP URL 推导目标。"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from blue.control.model import ServiceMeta
from blue.errors import ConfigError

__all__ = ["parse_host_port", "tcp_target_from_url", "wait_ready"]


def parse_host_port(value: str) -> tuple[str, int]:
    """解析 ``host:port`` / ``[::1]:port``。

    :raises ConfigError: 格式非法或端口越界。
    """
    text = value.strip()
    if not text:
        raise ConfigError("TCP 就绪目标不能为空")
    if text.startswith("["):
        host, separator, rest = text[1:].partition("]")
        if not separator or not rest.startswith(":"):
            raise ConfigError(f"TCP 就绪目标格式错误：{value!r}")
        port_text = rest[1:]
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator:
            raise ConfigError(f"TCP 就绪目标缺少端口：{value!r}")
    if not host:
        raise ConfigError(f"TCP 就绪目标缺少主机：{value!r}")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ConfigError(f"TCP 就绪端口不是整数：{value!r}") from error
    if not 0 < port < 65536:
        raise ConfigError(f"TCP 就绪端口越界：{port}")
    return host, port


def tcp_target_from_url(url: str) -> tuple[str, int] | None:
    """从 ``amqp://host:port/vhost`` 提取 TCP 目标。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or 5672
    return host, port


def readiness_target(meta: ServiceMeta, config: dict[str, Any]) -> str | None:
    """计算服务的 TCP 就绪目标。

    显式 ``ready_tcp`` 优先；对于 ``bus`` 服务，缺省从 ``proxy_listen``
    或 ``url`` 推导，避免在元数据中重复端口配置。
    """
    if meta.ready_tcp:
        return meta.ready_tcp
    if meta.name != "bus":
        return None
    if bool(config.get("proxy")) and isinstance(config.get("proxy_listen"), str):
        url = config["proxy_listen"]
    else:
        url = config.get("url")
    if not isinstance(url, str):
        return None
    target = tcp_target_from_url(url)
    if target is None:
        return None
    host, port = target
    return f"{host}:{port}"


async def wait_ready(
    process: asyncio.subprocess.Process,
    meta: ServiceMeta,
    *,
    timeout: float,
    target: str | None = None,
) -> bool:
    """等待服务就绪。

    - ``none``：立即就绪；
    - ``process``：短暂观察后进程仍存活即就绪；
    - ``tcp``：在超时内反复连接目标。
    """
    if meta.readiness == "none":
        return True
    if meta.readiness == "process":
        # 观察期可配（``service.infd`` 的 ``ready_settle``）：只观察一个固定短窗口
        # 猜不出「起来了」和「起来就崩」，而 python 解释器本身启动就要 30~100ms。
        await asyncio.sleep(min(meta.ready_settle, timeout))
        return process.returncode is None
    if meta.readiness == "tcp":
        if target is None:
            return False
        host, port = parse_host_port(target)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                return False
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=0.5
                )
            except OSError, TimeoutError:
                await asyncio.sleep(0.2)
                continue
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        return False
    return process.returncode is None
