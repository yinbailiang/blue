"""会话层测试：创建、快照、重置与过期回收。"""

from __future__ import annotations

from infinity_agent import MessageRole

from blue.session import SessionStore


class FakeClock:
    """可手动推进的时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def test_get_creates_session_with_system_message() -> None:
    """新会话以 system 消息初始化。"""
    store = SessionStore(system_prompt="系统提示")

    session = store.get("s1")

    assert session.session_id == "s1"
    assert [message.role for message in session.messages] == [MessageRole.SYSTEM]
    assert store.get("s1") is session


def test_snapshot_sorted_by_recent_activity() -> None:
    """快照按最后更新时间倒序。"""
    clock = FakeClock()
    store = SessionStore(system_prompt="p", now=clock)

    store.get("old").touch(now=clock.value)
    clock.value += 10
    store.get("new").touch(now=clock.value)

    assert [info.session_id for info in store.snapshot()] == ["new", "old"]


def test_reset_removes_session() -> None:
    """重置后会话消失，再访问会重建。"""
    store = SessionStore(system_prompt="p")
    store.get("s1")

    assert store.reset("s1") is True
    assert store.reset("s1") is False
    assert len(store) == 0


def test_prune_removes_idle_sessions() -> None:
    """超过空闲阈值的会话被回收。"""
    clock = FakeClock()
    store = SessionStore(system_prompt="p", idle_ttl=60.0, now=clock)
    store.get("idle")
    clock.value += 120
    store.get("active")

    removed = store.prune()

    assert removed == ["idle"]
    assert list(store.sessions) == ["active"]


def test_store_evicts_oldest_when_over_capacity() -> None:
    """超过容量上限时淘汰最久未活动的会话。"""
    clock = FakeClock()
    store = SessionStore(system_prompt="p", max_sessions=2, now=clock)

    store.get("a")
    clock.value += 1
    store.get("b")
    clock.value += 1
    store.get("c")

    assert set(store.sessions) == {"b", "c"}
