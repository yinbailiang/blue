"""测试辅助：进程内总线运行时、脚本化 LLM 客户端、内存会话网关与等待工具。"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncGenerator, Callable, Iterable

from blue_contracts_session import EVENTS as SESSION_EVENTS
from blue_contracts_session import (
    QueuedMessage,
    SessionAttachmentBlob,
    SessionNode,
    SessionSnapshot,
)
from blue_contracts_tool import EVENTS as TOOL_EVENTS
from blue_service_agent.session_gateway import AttachmentPayload
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
from infinity_bus import EventBus

from blue.messages import decode_messages, encode_messages
from blue.transport import MemoryFanout, MemoryRuntime
from blue.tree import ROOT_ID, Tree


def make_runtime(hub: MemoryFanout, member_id: str) -> MemoryRuntime:
    """在共享 hub 上创建一个进程内总线成员。"""
    return MemoryRuntime(
        hub=hub,
        member_id=member_id,
        events=(*TOOL_EVENTS, *SESSION_EVENTS),
    )


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


class MemorySessionGateway:
    """测试用内存会话网关：与 session 服务同一套树语义。

    节点 id 按插入顺序自增；``append`` 的 ``parent`` 与线上一致
    （``None`` = 当前链末端），一次追加拿到的最后一个 id 就是新的链尾。
    ``messages`` 给出每个会话当前链上的消息，方便断言「模型看到了什么」。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, list[SessionNode]] = {}
        self._root_current: dict[str, int | None] = {}
        self._next_id = 1
        self.steps: dict[str, int] = {}
        self.blobs: dict[str, AttachmentPayload] = {}
        self.queue: dict[str, list[QueuedMessage]] = {}
        self._queue_seq = 0

    # ---------- 待发送队列（与 session 服务同一套状态机语义） ----------

    def enqueue(self, session_id: str, text: str) -> QueuedMessage:
        """往某个会话的队列里塞一条（测试用）。"""
        self._queue_seq += 1
        message = QueuedMessage(
            id=f"q{self._queue_seq}",
            session_id=session_id,
            text=text,
            position=self._queue_seq,
        )
        self.queue.setdefault(session_id, []).append(message)
        return message

    def queued(self, session_id: str) -> list[QueuedMessage]:
        """该会话当前的队列内容。"""
        return list(self.queue.get(session_id, []))

    async def claim(
        self, bus_proxy: EventBus.Proxy, session_id: str
    ) -> QueuedMessage | None:
        """认领队首待发（pending → sending），与线上语义一致。"""
        del bus_proxy
        for message in self.queue.get(session_id, []):
            if message.state != "pending":
                continue
            message.state = "sending"
            message.attempts += 1
            return message
        return None

    async def complete(self, bus_proxy: EventBus.Proxy, message_id: str) -> None:
        """认领的消息跑完了。"""
        del bus_proxy
        for session_id, items in self.queue.items():
            remaining = [item for item in items if item.id != message_id]
            if len(remaining) != len(items):
                self.queue[session_id] = remaining
                return

    async def fail(
        self, bus_proxy: EventBus.Proxy, message_id: str, error: str
    ) -> None:
        """认领的消息失败了。"""
        del bus_proxy
        for items in self.queue.values():
            for item in items:
                if item.id == message_id:
                    item.state = "failed"
                    item.error = error

    async def retry(
        self, bus_proxy: EventBus.Proxy, session_id: str, message_id: str
    ) -> None:
        """把失败项放回待发。"""
        del bus_proxy
        for item in self.queue.get(session_id, []):
            if item.id == message_id:
                item.state = "pending"
                item.error = None

    async def queued_sessions(self, bus_proxy: EventBus.Proxy) -> list[str]:
        """哪些会话还有待发消息。"""
        del bus_proxy
        return [
            session_id
            for session_id, items in self.queue.items()
            if any(item.state == "pending" for item in items)
        ]

    @property
    def messages(self) -> dict[str, list[Message]]:
        """每个会话当前链上的消息。"""
        return {
            session_id: decode_messages(
                [node.message for node in self.chain(session_id)]
            )
            for session_id in self._nodes
        }

    def tree(self, session_id: str) -> Tree[SessionNode]:
        """该会话的树视图。"""
        return Tree(
            self._nodes.get(session_id, []),
            root_current=self._root_current.get(session_id),
        )

    def chain(self, session_id: str) -> list[SessionNode]:
        """当前链上的节点。"""
        tree = self.tree(session_id)
        return tree.collect(tree.chain())

    def seed(self, session_id: str, messages: Iterable[Message]) -> list[int]:
        """把一组消息装成一条单链（测试预置历史用）。"""
        return self._append(session_id, list(messages), parent=None)

    async def ensure(
        self, bus_proxy: EventBus.Proxy, session_id: str, system_prompt: str
    ) -> SessionSnapshot:
        del bus_proxy
        if session_id not in self._nodes:
            self._nodes[session_id] = []
            self._root_current[session_id] = None
            if system_prompt:
                self._append(session_id, [Message.system(system_prompt)], parent=None)
        return self._snapshot(session_id)

    async def append(
        self,
        bus_proxy: EventBus.Proxy,
        session_id: str,
        messages: list[Message],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> list[int]:
        del bus_proxy
        for blob in attachments:
            self.blobs[blob.hash] = AttachmentPayload(
                hash=blob.hash,
                mime=blob.mime,
                size=len(blob.data),
                data=blob.data,
            )
        return self._append(session_id, messages, parent=parent)

    async def get_attachment(
        self, bus_proxy: EventBus.Proxy, digest: str
    ) -> AttachmentPayload | None:
        del bus_proxy
        return self.blobs.get(digest)

    async def update(
        self, bus_proxy: EventBus.Proxy, session_id: str, steps: int
    ) -> None:
        del bus_proxy
        self.steps[session_id] = steps

    def _append(
        self, session_id: str, messages: list[Message], *, parent: int | None
    ) -> list[int]:
        """按真实语义挂链：显式 parent 优先，否则挂到当前链末端。"""
        nodes = self._nodes.setdefault(session_id, [])
        self._root_current.setdefault(session_id, None)
        mount = parent
        if mount is None:
            chain = self.tree(session_id).chain()
            mount = chain[-1] if chain else ROOT_ID
        created: list[int] = []
        previous = mount
        for message in messages:
            node = SessionNode(
                id=self._next_id,
                parent=previous,
                message=encode_messages([message])[0],
            )
            self._next_id += 1
            nodes.append(node)
            self._select(session_id, previous, node.id)
            previous = node.id
            created.append(node.id)
        return created

    def _select(self, session_id: str, parent: int, child: int) -> None:
        """把父节点的当前分支指向 ``child``。"""
        if parent == ROOT_ID:
            self._root_current[session_id] = child
            return
        for node in self._nodes[session_id]:
            if node.id == parent:
                node.current = child
                return

    def _snapshot(self, session_id: str) -> SessionSnapshot:
        tree = self.tree(session_id)
        return SessionSnapshot(
            session_id=session_id,
            nodes=list(self._nodes.get(session_id, [])),
            current=tree.chain(),
            root_current=self._root_current.get(session_id),
            created_at=0.0,
            updated_at=0.0,
            steps=self.steps.get(session_id, 0),
            message_count=len(self._nodes.get(session_id, [])),
        )
