"""事件去重中间件：在处理器**获得执行权的那一刻**做一次性认领。

at-least-once 语义下（未 ack 断连重投 / ``restart`` 补投）事件可能被重复投递，事件自带
唯一 ``Event.id``（uuid4 hex）作去重键。

**认领写在哪，决定了崩溃时会发生什么**：``before_dispatch`` 只查不写，``on_task_start``
才落库。于是——

- 认领落库时处理器已经拿到执行权；此后成功、失败、超时还是被取消，都与去重无关
  ——那是执行语义（重试 / 补偿）的事。
- 派发之后、启动之前崩溃（supervisor 到点 SIGKILL、掉电），库里没有任何记录，broker
  重投后照常派发，事件不会丢。反过来，认领若写在 ``before_dispatch``，这个窗口里的事件
  会被自己的认领挡住：既不执行、又不会 ack，变成持久队列上的毒消息。
- 处理器身份必须**跨重启稳定**——``EventHandlerRegistry.register()`` 返回的
  ``handler_id`` 是每次注册新生成的 uuid，重启后与去重表对不上，绝不能拿它当键；这里用
  「模块.类名」，并允许处理器用 ``dedupe_key`` 属性显式覆盖（同类多实例、或类名会重命名
  时用）。

由此有两条必须知道的后果：

1. **失败不自动重试**：认领在启动时就写死了，重投回来会被挡掉。失败由
   ``TaskErrorEvent`` 上报；要重试得另配机制（``release`` + 显式重发重试事件）。
2. **重复投递必须被 ack 掉**：``before_dispatch`` 把空列表交给核心层，由它确认消息。
   不确认的话，持久队列上会留下永不 ack 的消息——每次重连重投一次，还占着 prefetch
   配额。同一进程内还有原始任务排队等执行权时例外：这条重复保持未确认，因为原任务若
   在启动前被放弃，事件还得靠它重投。

认领粒度是 **per-handler**：上游 ``claim(consumer, event_id)`` 是事件级的，把处理器身份
编进 ``consumer``（见 :func:`consumer_key`）才能在 blue 侧拿到「只补跑没起来的那个」的
精度——重投时已启动的处理器有认领、没启动的没有，两边自然分开。

存储交给库的 :class:`~infinity_bus.templates.SqliteIdempotencyRecorder`（WAL + 保留期
清理），blue 不再自己实现一份。
"""

from __future__ import annotations

from typing import Any

from infinity_bus import (
    BeforePublishNext,
    DispatchNext,
    Event,
    EventHandler,
    EventHandlerRegistry,
    EventRegistry,
    Middleware,
    OnPublishNext,
    TaskRecord,
)
from infinity_bus.templates import IdempotencyRecorder
from pydantic import BaseModel

__all__ = ["IdempotencyMiddleware", "consumer_key", "handler_dedupe_key"]


def handler_dedupe_key(handler: EventHandler) -> str:
    """处理器的**稳定**去重键：显式 ``dedupe_key`` 优先，否则「模块.类名」。

    为什么不用 ``handler.handler_id``：那是 ``EventHandlerRegistry.register()`` 每次
    生成的 ``uuid4().hex``，进程重启就换一个——去重表里的旧记录永远匹配不上，
    跨重启去重会静默失效。

    需要区分的场景（同一个类注册多个实例）在处理器上声明 ``dedupe_key`` 即可：

    ```python
    class OutboxHandler(EventHandler):
        dedupe_key = "outbox:session"   # 实例级也可以：self.dedupe_key = ...
    ```
    """
    explicit = getattr(handler, "dedupe_key", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    cls = type(handler)
    return f"{cls.__module__}.{cls.__qualname__}"


def consumer_key(consumer: str, handler: EventHandler) -> str:
    """该处理器在成员 ``consumer`` 上的认领键：``"{member_id}:{稳定键}"``。

    认领表主键是 ``(consumer, event_id)``，把处理器身份编进 ``consumer`` 才有
    per-handler 精度。业务侧要手动 ``release``（见模块 docstring 的重试说明）时，
    也用这个函数拼键，别自己拼字符串。
    """
    return f"{consumer}:{handler_dedupe_key(handler)}"


class IdempotencyMiddleware(Middleware):
    """按「消费方 + 处理器」维度保证事件**只被执行一次**。

    状态机（只记「已派发、还没拿到执行权」的事件，两类出口都会清，不泄漏）：

    - ``before_dispatch``：先查库（已经执行过的重复），再查 ``_dispatched``（本进程内
      还在排队等执行权的重复），只把没见过的处理器交给 ``next``。**认领不在这里落库**
      ——此时崩溃不该留下任何痕迹。若一个都不剩，仍把空列表交给 ``next``，让核心层
      ack 这条重复；只有「原任务还在飞」时才保持未确认。
    - ``on_task_start``：处理器真正获得执行权，这时才 ``claim`` 落库并清掉跟踪条目。
    - ``on_dispatch_abandoned``：只清内存跟踪。没起来的处理器本来就没有认领（无需
      ``release``），已启动的处理器保留认领（重投不该再跑一遍）。
    """

    def __init__(
        self,
        recorder: IdempotencyRecorder,
        *,
        consumer: str,
        handlers: EventHandlerRegistry,
    ) -> None:
        """``consumer`` 是本成员标识（fanout 广播下各成员各自认领，互不串扰）。"""
        self._recorder = recorder
        self._consumer = consumer
        self._handlers = handlers
        # event.id → {handler_id: 认领键}：已交给处理器、但还没获得执行权
        self._dispatched: dict[str, dict[str, str]] = {}

    @property
    def tracked_events(self) -> int:
        """还有未结清跟踪状态的事件数（派发中 / 等待启动）——观测与测试用。"""
        return len(self._dispatched)

    async def before_publish(
        self,
        event_registry: EventRegistry,
        name: str,
        source: str,
        data: dict[str, Any] | BaseModel | None,
        old_event: Event | None,
        next: BeforePublishNext,
    ) -> None:
        """本中间件不参与发布链路，原样放行。"""
        await next(event_registry, name, source, data, old_event)

    async def on_publish(self, event: Event, next: OnPublishNext) -> None:
        """本中间件不参与发布链路，原样放行。"""
        await next(event)

    async def before_dispatch(
        self,
        event: Event,
        handler_ids: list[str],
        next: DispatchNext,
    ) -> None:
        """派发没执行过的处理器；全是重复就把空列表交给核心层（让它 ack）。"""
        in_flight = self._dispatched.get(event.id, {})
        pending: dict[str, str] = {}
        keep: list[str] = []
        awaiting_in_flight = False
        for handler_id in handler_ids:
            handler = self._handlers.get(handler_id)
            if handler is None:
                continue
            key = consumer_key(self._consumer, handler)
            if handler_id in in_flight:
                # 本进程内同一条事件的重复投递（连接抖动后 broker 重投）：原任务还在
                # 排队等执行权。这条重复不能 ack —— 原任务若在启动前被放弃，事件还
                # 得靠它重投；等它真的启动或放弃之后，下一次投递自然会被判成重复。
                awaiting_in_flight = True
                continue
            if await self._recorder.is_processed(key, event.id):
                continue
            pending[handler_id] = key
            keep.append(handler_id)
        if pending:
            self._dispatched.setdefault(event.id, {}).update(pending)
        if keep:
            await next(event, keep)
        elif not awaiting_in_flight:
            # 全是已执行过的重复（或没有处理器接手）：空列表走到核心层，由它 ack。
            await next(event, [])

    async def on_task_start(self, record: TaskRecord) -> None:
        """处理器拿到执行权 → 这时才把认领写进库（崩溃窗口里没有记录可残留）。"""
        pending = self._dispatched.get(record.event.id)
        if pending is None:
            return
        key = pending.pop(record.handler_id, None)
        if not pending:
            self._dispatched.pop(record.event.id, None)
        if key is not None:
            await self._recorder.claim(key, record.event.id)

    async def on_dispatch_abandoned(self, event: Event) -> None:
        """派发被放弃：清掉内存跟踪即可。

        认领只在处理器启动时写，没起来的那些处理器**没有**认领需要撤回；已启动的
        处理器保留认领——它们已经拿到执行权，重投不该再跑一遍。
        """
        self._dispatched.pop(event.id, None)
