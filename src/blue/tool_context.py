"""工具调用上下文：让工具函数知道自己在为哪个会话、哪次请求干活。

工具函数的入参 schema 是**发给模型看**的，不能往里塞 ``session_id`` 这类内部字段；
所以上下文走 ``ContextVar``：``ToolWorker`` 在调用前绑定、调用后复位，asyncio 下
每个任务各持一份，并发调用不会串。

工具里这样用：

.. code-block:: python

    from blue.tool_context import call_service, current_context

    async def history_search(query: str) -> SearchResult:
        context = current_context()
        if not context.session_id:
            raise RuntimeError("history_search 只能在会话内调用")
        reply = await call_service(
            SessionSearchEvent.name,
            {"query": query},
            SessionSearchReplyEvent.name,
            session_id=context.session_id,
        )

``call_service`` 需要工具进程把总线运行时绑进来（``run_tool`` 会自动做）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from infinity_bus.templates.request import request

from blue.transport import BusRuntime

__all__ = [
    "ToolContext",
    "bind_runtime",
    "call_service",
    "current_context",
    "proxy",
    "session_proxy",
    "use_context",
]

_CTX: ContextVar[ToolContext] = ContextVar("blue_tool_context")
"""当前工具调用上下文；不在工具调用里时取到的是空上下文。"""

_RUNTIME: ContextVar[BusRuntime | None] = ContextVar("blue_tool_runtime", default=None)
"""工具进程绑定的总线运行时，供工具函数反向调用别的服务。"""


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的上下文。"""

    session_id: str | None = None
    """调用发生在哪个会话；工具如果与会话无关就是 None。"""

    request_id: str = ""
    """发起这次调用的请求 id（同一轮对话里多次工具调用共用）。"""

    tool: str = ""
    """工具名。"""


_EMPTY = ToolContext()
"""空上下文：工具被单独调用（测试、脚本）时用它。"""


def current_context() -> ToolContext:
    """取当前工具调用的上下文（不在工具调用里时返回空上下文）。"""
    return _CTX.get(_EMPTY)


@contextmanager
def use_context(context: ToolContext) -> Any:
    """绑定上下文，退出时恢复原值（``ToolWorker`` 内部使用）。"""
    token: Token[ToolContext] = _CTX.set(context)
    try:
        yield
    finally:
        _CTX.reset(token)


def session_proxy() -> Any:
    """拿一个指向会话服务的强类型代理（工具里最常用的反向调用）。

    契约包提供 ``SessionProxy``；这里只是把它绑到工具进程的总线出口上，
    省得每个工具都自己 import 契约 + 取 runtime + 拼 proxy。
    """
    from blue_contracts_session import SessionProxy

    return SessionProxy(proxy())


def proxy() -> Any:
    """当前工具进程的总线出口（未绑定运行时则报错）。"""
    runtime = _RUNTIME.get()
    if runtime is None:
        raise RuntimeError(
            "工具进程未绑定总线运行时：请用 run_tool 启动，或先调用 bind_runtime"
        )
    return runtime.proxy(runtime.member_id)


def bind_runtime(runtime: BusRuntime) -> None:
    """把工具进程的总线运行时绑进来，供 :func:`call_service` 使用。"""
    _RUNTIME.set(runtime)


def bind_runtime_scoped(runtime: BusRuntime) -> Any:
    """绑定运行时并返回一个上下文管理器（测试与 ``run_tool`` 使用）。"""

    @contextmanager
    def _scope() -> Any:
        token: Token[BusRuntime | None] = _RUNTIME.set(runtime)
        try:
            yield
        finally:
            _RUNTIME.reset(token)

    return _scope()


async def call_service(
    event: str,
    payload: dict[str, Any],
    reply_event: str,
    *,
    session_id: str | None = None,
    timeout: float = 10.0,
) -> Any:
    """从工具进程内向别的服务发一次请求。

    :param event: 请求事件名。
    :param payload: 请求体。
    :param reply_event: 应答事件名。
    :param session_id: 会话 id（``RequestProtocol`` 要求，与会话无关的工具可省略）。
    :param timeout: 超时秒数。
    """
    runtime = _RUNTIME.get()
    if runtime is None:
        raise RuntimeError(
            "工具进程未绑定总线运行时：请用 run_tool 启动，或先调用 bind_runtime"
        )
    proxy = runtime.proxy(runtime.member_id)
    return await request(
        proxy, event, payload, reply_event, session_id=session_id, timeout=timeout
    )
