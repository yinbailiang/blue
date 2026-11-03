"""工具登记表测试：租约、版本号与同名接管。"""

from __future__ import annotations

from infinity_agent import ToolDefinition

from blue.catalog import ToolRegistryState


def _tool(name: str, description: str = "") -> ToolDefinition:
    return ToolDefinition.create(name, description)


def test_register_adds_tool_and_bumps_version() -> None:
    """新工具登记后目录版本递增。"""
    state = ToolRegistryState()

    changed = state.register("w1", _tool("echo"), ttl=10.0, now=100.0)

    assert changed is True
    assert state.version == 1
    assert [tool.function.name for tool in state.tools()] == ["echo"]


def test_heartbeat_does_not_change_version() -> None:
    """续约只延长租约，不改变目录。"""
    state = ToolRegistryState()
    state.register("w1", _tool("echo"), ttl=10.0, now=100.0)

    assert state.heartbeat("w1", ttl=10.0, now=105.0) is True
    assert state.version == 1
    record = state.record("w1")
    assert record is not None
    assert record.expires_at == 115.0


def test_heartbeat_unknown_worker_returns_false() -> None:
    """未登记进程的续约失败，调用方应要求重新注册。"""
    state = ToolRegistryState()

    assert state.heartbeat("ghost", ttl=10.0) is False


def test_same_tool_from_new_worker_takes_over() -> None:
    """同名工具以最新登记的进程为准，旧记录被移除。"""
    state = ToolRegistryState()
    state.register("w1", _tool("echo", "旧"), ttl=10.0, now=100.0)

    changed = state.register("w2", _tool("echo", "新"), ttl=10.0, now=101.0)

    assert changed is True
    assert state.worker_count == 1
    assert state.record("w1") is None
    assert state.tools()[0].function.description == "新"


def test_sweep_removes_expired_workers() -> None:
    """租约到期后工具从目录中移除。"""
    state = ToolRegistryState()
    state.register("w1", _tool("echo"), ttl=10.0, now=100.0)

    assert state.sweep(now=105.0) == []
    assert state.sweep(now=111.0) == ["w1"]
    assert state.tools() == []
    assert state.version == 2


def test_unregister_removes_worker() -> None:
    """主动下线会移除记录并递增版本。"""
    state = ToolRegistryState()
    state.register("w1", _tool("echo"), ttl=10.0, now=100.0)

    assert state.unregister("w1") is True
    assert state.unregister("w1") is False
    assert state.worker_count == 0
    assert state.version == 2
