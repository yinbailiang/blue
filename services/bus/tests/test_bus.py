"""bus 服务测试：URL 解析、外部模式与 TCP 代理。"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from blue_service_bus.config import BusConfig
from blue_service_bus.proxy import TcpProxy
from blue_service_bus.rabbitmq import (
    RabbitMQManager,
    parse_amqp_url,
    resolve_password,
)

from blue.errors import RuntimeConfigError


def test_parse_amqp_url() -> None:
    """解析主机、端口与 vhost。"""
    endpoint = parse_amqp_url("amqp://user:pass@rabbit.local:5673/blue")
    assert endpoint.host == "rabbit.local"
    assert endpoint.port == 5673
    assert endpoint.vhost == "blue"


def test_parse_amqp_url_defaults() -> None:
    """缺省端口 5672，缺省 vhost 为 /。"""
    endpoint = parse_amqp_url("amqp://localhost")
    assert endpoint.port == 5672
    assert endpoint.vhost == "/"


def test_parse_amqp_url_rejects_invalid() -> None:
    """非法协议与缺少主机被拒绝。"""
    with pytest.raises(RuntimeConfigError):
        parse_amqp_url("http://localhost")
    with pytest.raises(RuntimeConfigError):
        parse_amqp_url("amqp:///vhost")


def test_resolve_password_prefers_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了密码就用配置值。"""
    monkeypatch.setenv("RABBITMQ_PASSWORD", "from-env")
    config = BusConfig(user="blue", password="from-secrets")

    assert resolve_password(config) == "from-secrets"


def test_resolve_password_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不再读环境变量：非 guest 用户未配置时报错。"""
    monkeypatch.setenv("RABBITMQ_PASSWORD", "from-env")

    with pytest.raises(RuntimeConfigError, match=r"secrets\.toml"):
        resolve_password(BusConfig(user="blue"))


def test_resolve_password_falls_back_for_guest() -> None:
    """guest 用户留空时使用 RabbitMQ 默认密码。"""
    assert resolve_password(BusConfig()) == "guest"
    assert resolve_password(BusConfig(password="")) == "guest"


def test_bus_config_defaults() -> None:
    """默认 managed 模式与本地 RabbitMQ。"""
    config = BusConfig()
    assert config.mode == "managed"
    assert config.transport == "rabbit"
    assert config.data_dir == Path("rabbitmq")


def test_external_mode_accepts_reachable_broker() -> None:
    """external 模式只探测端口，不启动子进程。"""
    asyncio.run(_external_mode())


async def _external_mode() -> None:
    port = _free_port()
    server = await asyncio.start_server(lambda r, w: _close(w), "127.0.0.1", port)
    try:
        config = BusConfig(
            mode="external",
            configure=False,
            url=f"amqp://guest:guest@127.0.0.1:{port}/blue",
            startup_timeout=2.0,
        )
        manager = RabbitMQManager(config)
        await manager.start()
        await manager.stop()
    finally:
        server.close()
        await server.wait_closed()


def test_tcp_proxy_forwards_bytes() -> None:
    """代理把客户端字节转发到上游并回传。"""
    asyncio.run(_proxy())


async def _proxy() -> None:
    upstream_port = _free_port()
    listen_port = _free_port()

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", upstream_port)
    config = BusConfig(
        mode="external",
        configure=False,
        proxy=True,
        url=f"amqp://guest:guest@127.0.0.1:{upstream_port}/blue",
        proxy_listen=f"amqp://127.0.0.1:{listen_port}/blue",
    )
    proxy = TcpProxy(config)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"ping"
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
