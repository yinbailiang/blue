"""测试辅助：进程内总线运行时、脚本化 LLM 客户端与等待工具。"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncGenerator, Callable

from infinity_agent import (
    DoneChunk,
    FinishChunk,
    Message,
    Messages,
    StreamChunk,
    TextChunk,
    ToolCallCompleteChunk,
)
from infinity_agent.clients import LLMClient, RequestConfig

from blue.protocol import TopicBook
from blue.transport import MemoryFanout, MemoryRuntime


def make_runtime(hub: MemoryFanout, member_id: str, topics: TopicBook) -> MemoryRuntime:
    """在共享 hub 上创建一个进程内总线成员。"""
    return MemoryRuntime(hub=hub, member_id=member_id, topics=topics)


class ScriptedClient(LLMClient):
    """按脚本逐轮返回 assistant 消息的假客户端。"""

    def __init__(self, script: list[Message]) -> None:
        self._script: list[Message] = list(script)
        self.calls: list[Messages] = []

    async def __aenter__(self) -> ScriptedClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> bool | None:
        return None

    async def stream_chat(
        self,
        messages: Messages,
        config: RequestConfig | None = None,
    ) -> AsyncGenerator[StreamChunk]:
        del config
        self.calls.append(list(messages))
        message = self._script.pop(0) if self._script else Message.assistant(content="")
        if isinstance(message.content, str) and message.content:
            yield TextChunk(text=message.content)
        if message.tool_calls:
            yield ToolCallCompleteChunk(tool_calls=message.tool_calls)
        yield FinishChunk(finish_reason="stop")
        yield DoneChunk()


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, interval: float = 0.01
) -> None:
    """轮询等待条件成立。

    :raises AssertionError: 超时（附上条件说明）。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待条件超时（{timeout}s）：{predicate}")
