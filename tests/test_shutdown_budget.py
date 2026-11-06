"""停机预算测试：库的排空超时必须落在 supervisor 的 ``stop_timeout`` 之内。

背景：``infinity_bus`` 的默认 ``ShutdownConfig`` 与 supervisor 的停止预算毫无关系
——队列空时 ``queue.join()`` 只等 1 秒，最长 15 秒，还随积压条数缩水。于是慢处理器
常常走不到优雅收尾，supervisor 到点 SIGKILL，未 ack 的消息被 broker 重投
（AMQP at-least-once），非幂等处理器就重复执行了。

这里盯的是不变量：``queue.join`` 预算 + 兜底等待 + 余量 ≤ ``stop_timeout``。
"""

from __future__ import annotations

from blue.transport import shutdown_config_for


def _budget_for(stop_timeout: float) -> float:
    """与 ``blue.runtime.shutdown_drain_timeout`` 同一套算法（留 2 秒退出余量）。"""
    return max(1.0, stop_timeout - 2.0)


def test_shutdown_config_uses_fixed_budget() -> None:
    """不管积压多长都给固定预算：``avg_wait_time=0`` 让超时不随 qsize 缩水。"""
    config = shutdown_config_for(8.0)

    assert config.queue_timeout_min == 8.0
    assert config.queue_timeout_max == 8.0
    assert config.avg_wait_time == 0.0
    # 库里的算法是 max(min_, min(max_, qsize * avg))；avg=0 时恒等于 min_
    assert (
        max(config.queue_timeout_min, min(config.queue_timeout_max, 999 * 0.0)) == 8.0
    )


def test_shutdown_budget_fits_inside_stop_timeout() -> None:
    """核心不变量：排空 + 兜底 + 余量必须装进 supervisor 的预算。

    超了就意味着 supervisor 会先 SIGKILL —— 那时 unacked 的消息必然被重投。
    """
    for stop_timeout in (3.0, 5.0, 10.0, 15.0, 30.0):
        budget = _budget_for(stop_timeout)
        config = shutdown_config_for(budget)
        worst = config.queue_timeout_max + config.tasks_timeout

        assert worst < stop_timeout, (
            f"stop_timeout={stop_timeout}s 装不下 {worst}s 的排空预算"
        )


def test_queue_gets_the_larger_share_of_budget() -> None:
    """大头要给 queue.join()：restart 策略下它负责等处理器收尾。"""
    config = shutdown_config_for(8.0)

    assert config.queue_timeout_max > config.tasks_timeout
