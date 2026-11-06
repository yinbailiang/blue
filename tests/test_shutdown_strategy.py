"""停机策略测试：默认 ``restart``，只有 ``down`` 才切 ``offline``。

背景：``offline`` 会解绑队列并排空——**重启期间发布的消息就此丢弃**。所以普通重启
必须走 ``restart``（保留积压、启动后补投），只有「真正下线」才切 ``offline``。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

from blue.runtime import build_runtime
from blue.transport import RabbitRuntime


class _FakeQueue:
    """只带 ``_strategy`` 字段的假队列（模拟 RabbitFanoutQueue 的内部实现）。"""

    def __init__(self) -> None:
        self._strategy = "restart"


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


def test_memory_runtime_accepts_strategy_switch() -> None:
    """内存运行时没有队列策略，切换应当是无害的 no-op。"""
    from blue.transport import MemoryFanout, MemoryRuntime

    runtime = MemoryRuntime(member_id="m", events=[], hub=MemoryFanout())

    runtime.set_shutdown_strategy("offline")  # 不应抛异常


def test_intent_file_switches_to_offline(tmp_path: Path) -> None:
    """读到下线意图就切 offline，而且文件读完即删（一次性）。"""
    intent = tmp_path / "shutdown-intent.json"
    intent.write_text('{"mode": "offline"}', encoding="utf-8")
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/",
        member_id="m",
        events=[],
        intent_path=intent,
    )
    queue = _FakeQueue()
    cast(Any, runtime)._queue = queue

    cast(Any, runtime)._before_bus_stop()

    assert vars(queue)["_strategy"] == "offline"
    assert not intent.exists(), "意图是一次性的，读完必须删掉，否则污染下次重启"


def test_missing_intent_file_keeps_restart(tmp_path: Path) -> None:
    """没有意图文件（普通重启）时必须保持 restart。"""
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/",
        member_id="m",
        events=[],
        intent_path=tmp_path / "absent.json",
    )
    queue = _FakeQueue()
    cast(Any, runtime)._queue = queue

    cast(Any, runtime)._before_bus_stop()

    assert vars(queue)["_strategy"] == "restart"


def test_non_offline_intent_keeps_restart(tmp_path: Path) -> None:
    """文件存在但写的不是 offline 时，不能被误切。"""
    intent = tmp_path / "shutdown-intent.json"
    intent.write_text('{"mode": "restart"}', encoding="utf-8")
    runtime = RabbitRuntime(
        url="amqp://guest:guest@localhost/",
        member_id="m",
        events=[],
        intent_path=intent,
    )
    queue = _FakeQueue()
    cast(Any, runtime)._queue = queue

    cast(Any, runtime)._before_bus_stop()

    assert vars(queue)["_strategy"] == "restart"
