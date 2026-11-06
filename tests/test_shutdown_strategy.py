"""停机策略测试：默认 ``restart``，只有 ``down`` 才切 ``offline``。

背景：``offline`` 会解绑队列并排空——**重启期间发布的消息就此丢弃**。所以普通重启
必须走 ``restart``（保留积压、启动后补投），只有「真正下线」才切 ``offline``。

意图文件按服务一份（``state_dir/shutdown-intent/<service>.json``）：共享一份时第一个
停机的服务读完即删，后面的服务全看不到——实测三个服务只有第一个走 offline。
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path
from typing import Any, cast

from infinity_bus.templates import EventCodec, RabbitFanoutQueue

from blue.runtime import (
    SHUTDOWN_INTENT_DIRNAME,
    build_runtime,
    clear_shutdown_intents,
    shutdown_intent_dir,
    shutdown_intent_path,
)
from blue.transport import SHUTDOWN_INTENT_TTL, RabbitRuntime


class _FakeQueue:
    """带公开 ``strategy`` / ``set_strategy()`` 的假队列（对齐 6.2 的队列 API）。"""

    def __init__(self) -> None:
        self._strategy = "restart"

    @property
    def strategy(self) -> str:
        """当前停机策略。"""
        return self._strategy

    def set_strategy(self, strategy: str) -> None:
        """切换停机策略。"""
        self._strategy = strategy


def _runtime(member: str, intent: Path) -> RabbitRuntime:
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/",
        member_id=member,
        events=[],
        intent_path=intent,
    )
    cast(Any, runtime)._queue = _FakeQueue()
    return runtime


def _strategy_of(runtime: RabbitRuntime) -> str:
    """读假队列的公开 ``strategy``（与 blue 实际走的是同一条路径）。"""
    return cast(str, cast(Any, runtime)._queue.strategy)


def _write_intent(path: Path, *, ts: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"mode": "offline"}
    if ts is not None:
        payload["ts"] = ts
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_default_strategy_is_restart() -> None:
    """``build_runtime`` 的默认策略必须是 restart。

    默认 offline 时，任何一次重启都会把停机期间的消息解绑丢弃——这正是最初
    「重启期间消息丢失」的根源。
    """
    signature = inspect.signature(build_runtime)

    assert signature.parameters["strategy"].default == "restart"


def test_set_shutdown_strategy_without_queue_is_safe() -> None:
    """队列还没建起来时切换策略不能炸（只是没有效果）。"""
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/", member_id="m", events=[]
    )

    runtime.set_shutdown_strategy("offline")  # 不应抛异常


def test_set_shutdown_strategy_updates_queue_field() -> None:
    """切策略要真的改到队列对象上——``join()`` 只在停机那一刻读这个字段。"""
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/", member_id="m", events=[]
    )
    queue = _FakeQueue()
    # 直接塞进私有槽：这里就是要验证 set_shutdown_strategy 怎么碰库的内部字段
    cast(Any, runtime)._queue = queue

    runtime.set_shutdown_strategy("offline")

    assert vars(queue)["_strategy"] == "offline"


def test_real_queue_exposes_public_strategy_api() -> None:
    """守卫：切策略用的是库的公开接口，库改名/移除必须让测试变红。

    上面几条用的是 ``_FakeQueue``，它自带 ``_strategy``（模拟 6.0 的老实现），只能
    验证降级路径。这里拿真的 ``RabbitFanoutQueue``（不连 broker，只构造）确认公开
    API 还在；库改了名这条会失败，而不是线上静默退回 ``restart``。
    """
    queue = RabbitFanoutQueue(
        None, None, None, None, "guard.member", EventCodec(), "restart"
    )
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/", member_id="guard.member", events=[]
    )
    cast(Any, runtime)._queue = queue

    assert hasattr(queue, "set_strategy"), (
        "库不再暴露 set_strategy：运行时切换停机策略会静默失效"
    )
    assert queue.strategy == "restart"

    runtime.set_shutdown_strategy("offline")

    assert queue.strategy == "offline"


def test_memory_runtime_accepts_strategy_switch() -> None:
    """内存运行时没有队列策略，切换应当是无害的 no-op。"""
    from blue.transport import MemoryFanout, MemoryRuntime

    runtime = MemoryRuntime(member_id="m", events=[], hub=MemoryFanout())

    runtime.set_shutdown_strategy("offline")  # 不应抛异常


def test_intent_file_switches_to_offline(tmp_path: Path) -> None:
    """读到下线意图就切 offline，而且文件读完即删（一次性）。"""
    intent = tmp_path / "shutdown-intent.json"
    _write_intent(intent, ts=time.time())
    runtime = _runtime("m", intent)

    cast(Any, runtime)._before_bus_stop()

    assert _strategy_of(runtime) == "offline"
    assert not intent.exists(), "意图是一次性的，读完必须删掉，否则污染下次重启"


def test_missing_intent_file_keeps_restart(tmp_path: Path) -> None:
    """没有意图文件（普通重启）时必须保持 restart。"""
    runtime = _runtime("m", tmp_path / "absent.json")

    cast(Any, runtime)._before_bus_stop()

    assert _strategy_of(runtime) == "restart"


def test_non_offline_intent_keeps_restart(tmp_path: Path) -> None:
    """文件存在但写的不是 offline 时，不能被误切。"""
    intent = tmp_path / "shutdown-intent.json"
    intent.write_text('{"mode": "restart"}', encoding="utf-8")
    runtime = _runtime("m", intent)

    cast(Any, runtime)._before_bus_stop()

    assert _strategy_of(runtime) == "restart"


def test_stale_intent_is_ignored(tmp_path: Path) -> None:
    """过期意图不生效：它是上一次 down 被强杀留下的残骸，照它切会丢积压。"""
    intent = tmp_path / "shutdown-intent.json"
    _write_intent(intent, ts=time.time() - SHUTDOWN_INTENT_TTL - 60.0)
    runtime = _runtime("m", intent)

    cast(Any, runtime)._before_bus_stop()

    assert _strategy_of(runtime) == "restart"
    assert not intent.exists(), "过期文件也要清掉，别留给下一次停机"


def test_every_service_sees_its_own_intent(tmp_path: Path) -> None:
    """按服务分文件：三个服务依次停机，三个都必须走 offline。

    共享一份文件时第一个服务读完就删，后面两个按 restart 留着积压——`blue down`
    之后队列里还留着本不该留的消息。
    """
    strategies: list[str] = []
    for name in ("service-a", "service-b", "service-c"):
        intent = shutdown_intent_path(name, state_dir=tmp_path)
        _write_intent(intent, ts=time.time())
        runtime = _runtime(name, intent)
        cast(Any, runtime)._before_bus_stop()
        strategies.append(_strategy_of(runtime))

    assert strategies == ["offline", "offline", "offline"]


def test_intent_paths_are_per_service(tmp_path: Path) -> None:
    """路径拼装：``state_dir/shutdown-intent/<service>.json``，服务名不重复。"""
    alpha = shutdown_intent_path("alpha", state_dir=tmp_path)
    beta = shutdown_intent_path("beta", state_dir=tmp_path)

    assert alpha != beta
    assert alpha.name == "alpha.json"
    assert alpha.parent == shutdown_intent_dir(tmp_path)
    assert alpha.parent.name == SHUTDOWN_INTENT_DIRNAME


def test_clear_shutdown_intents_removes_leftovers(tmp_path: Path) -> None:
    """supervisor 启动时清残留：上一次 down 没停完留下的文件必须消失。"""
    for name in ("alpha", "beta"):
        _write_intent(shutdown_intent_path(name, state_dir=tmp_path), ts=0.0)

    removed = clear_shutdown_intents(tmp_path)

    assert sorted(removed) == ["alpha", "beta"]
    assert list(shutdown_intent_dir(tmp_path).glob("*.json")) == []


def test_clear_shutdown_intents_is_safe_without_directory(tmp_path: Path) -> None:
    """没有意图目录时直接返回空（首次启动的常态）。"""
    assert clear_shutdown_intents(tmp_path) == []
