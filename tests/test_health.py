"""就绪探针测试：URL 转 TCP 目标与端口解析。"""

from __future__ import annotations

import pytest

from blue.control.health import parse_host_port, readiness_target, tcp_target_from_url
from blue.control.model import ServiceMeta
from blue.errors import ConfigError


def _bus_meta() -> ServiceMeta:
    return ServiceMeta(
        name="bus",
        kind="infra",
        package="demo-bus",
        module="demo_bus",
        readiness="tcp",
    )


def test_parse_host_port() -> None:
    """普通与 IPv6 形式都能解析。"""
    assert parse_host_port("127.0.0.1:5672") == ("127.0.0.1", 5672)
    assert parse_host_port("[::1]:5672") == ("::1", 5672)


def test_parse_host_port_rejects_invalid() -> None:
    """缺少端口与端口越界被拒绝。"""
    with pytest.raises(ConfigError):
        parse_host_port("localhost")
    with pytest.raises(ConfigError):
        parse_host_port("localhost:70000")


def test_tcp_target_from_amqp_url() -> None:
    """从 AMQP URL 提取主机与端口。"""
    assert tcp_target_from_url("amqp://user:pass@rabbit:5673/blue") == (
        "rabbit",
        5673,
    )
    assert tcp_target_from_url("amqp://localhost/blue") == ("localhost", 5672)


def test_readiness_target_uses_proxy_when_enabled() -> None:
    """bus 开启代理时探针指向代理端口。"""
    target = readiness_target(
        _bus_meta(),
        {
            "url": "amqp://guest:guest@127.0.0.1:5672/blue",
            "proxy": True,
            "proxy_listen": "amqp://127.0.0.1:5673/blue",
        },
    )
    assert target == "127.0.0.1:5673"


def test_readiness_target_defaults_to_upstream() -> None:
    """未开启代理时探针指向上游端口。"""
    target = readiness_target(
        _bus_meta(),
        {
            "url": "amqp://guest:guest@127.0.0.1:5672/blue",
            "proxy": False,
        },
    )
    assert target == "127.0.0.1:5672"


def test_readiness_target_ignores_non_bus() -> None:
    """非 bus 服务没有隐式 TCP 探针。"""
    meta = ServiceMeta(
        name="echo",
        kind="tool",
        package="demo-echo",
        module="demo_echo",
        readiness="tcp",
    )
    assert readiness_target(meta, {"url": "amqp://localhost:5672"}) is None
