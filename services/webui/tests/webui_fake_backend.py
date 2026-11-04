"""WebUI 测试替身后端：不连总线，按真实语义维护一棵小对话树。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from blue_contracts_session import (
    QueuedMessage,
    SessionActivity,
    SessionActivityPayload,
    SessionAttachment,
    SessionInfo,
    SessionNode,
    SessionOutboxPayload,
    SessionReplyResponse,
    SessionSnapshot,
    SessionStreamPayload,
)
from blue_contracts_tool import CatalogQueryResponse
from blue_service_webui.backend import ServiceView, SystemView
from infinity_agent import Message, ToolDefinition

from blue.messages import encode_message
from blue.tree import Tree


class FakeBackend:
    """不连接总线的测试替身。"""

    def __init__(self) -> None:
        self.reset_calls: list[str] = []
        self.last_attachments: list[SessionAttachment] = []
        self.blobs: dict[str, tuple[str, bytes]] = {}
        self.cancel_calls: list[str] = []
        self.stream_queue: asyncio.Queue[SessionStreamPayload] | None = None
        self.activities: list[SessionActivity] = []
        self.activity_queue: asyncio.Queue[SessionActivityPayload] | None = None
        self.pending_stream: SessionStreamPayload | None = None
        # 一棵最小的树：system → user → assistant，外加 user 下的第二条回答分支
        self.nodes: list[SessionNode] = [
            SessionNode(
                id=1, parent=0, message=encode_message(Message.system("系统提示"))
            ),
            SessionNode(id=2, parent=1, message=encode_message(Message.user("你好"))),
            SessionNode(
                id=3,
                parent=2,
                message=encode_message(Message.assistant(content="你好，我是 Blue。")),
            ),
            SessionNode(
                id=4,
                parent=2,
                message=encode_message(Message.assistant(content="另一个分支")),
            ),
        ]
        self.root_current: int | None = 1
        self.activation_calls: list[tuple[str, int]] = []
        self.delete_calls: list[tuple[str, int]] = []
        self.last_prompt: dict[str, Any] = {}
        self.title = "自动标题"
        self.preview = "最后一条用户消息"
        self.rename_calls: list[tuple[str, str]] = []
        self.outbox_items: list[QueuedMessage] = []
        self.outbox_queue: asyncio.Queue[SessionOutboxPayload] | None = None
        self.queue_calls: list[tuple[str, str, str, str]] = []
        # 默认选中的分支：system(1) → user(2) → assistant(3)
        self.nodes[0].current = 2
        self.nodes[1].current = 3

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def cancel(self, session_id: str) -> bool:
        self.cancel_calls.append(session_id)
        return False

    async def sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id="s1",
                message_count=2,
                updated_at=1_700_000_000.0,
                title=self.title,
                preview=self.preview,
                queued=len(self.outbox_items),
            )
        ]

    async def outbox(self, session_id: str) -> list[QueuedMessage]:
        """读取队列（替身：直接给内存里的列表）。"""
        del session_id
        return list(self.outbox_items)

    async def update_queued(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
    ) -> bool:
        """改队列项正文。"""
        del attachments
        self.queue_calls.append(("update", session_id, message_id, text))
        for item in self.outbox_items:
            if item.id == message_id:
                item.text = text
                return True
        return False

    async def move_queued(self, session_id: str, message_id: str, to: str) -> bool:
        """调整顺序。"""
        self.queue_calls.append(("move", session_id, message_id, to))
        return True

    async def send_queued_now(self, session_id: str, message_id: str) -> bool:
        """立刻发送。"""
        self.queue_calls.append(("send_now", session_id, message_id, ""))
        return True

    async def retry_queued(self, session_id: str, message_id: str) -> bool:
        """重试失败项。"""
        self.queue_calls.append(("retry", session_id, message_id, ""))
        return True

    async def clear_queued(self, session_id: str) -> int:
        """清空队列。"""
        self.queue_calls.append(("clear", session_id, "", ""))
        removed = len(self.outbox_items)
        self.outbox_items = []
        return removed

    async def remove_queued(self, session_id: str, message_id: str) -> bool:
        """取消一条。"""
        self.queue_calls.append(("remove", session_id, message_id, ""))
        before = len(self.outbox_items)
        self.outbox_items = [
            item for item in self.outbox_items if item.id != message_id
        ]
        return len(self.outbox_items) != before

    async def rename(self, session_id: str, title: str) -> str:
        """改名：空字符串表示恢复自动标题。"""
        self.rename_calls.append((session_id, title))
        self.title = title.strip() or "自动标题"
        return self.title

    @asynccontextmanager
    async def subscribe_outbox(
        self,
    ) -> AsyncGenerator[asyncio.Queue[SessionOutboxPayload]]:
        """订阅队列变化（测试里手动 push）。"""
        queue: asyncio.Queue[SessionOutboxPayload] = asyncio.Queue()
        self.outbox_queue = queue
        try:
            yield queue
        finally:
            self.outbox_queue = None

    def push_outbox(self) -> None:
        """把当前队列状态推给订阅者（模拟 session.outbox 广播）。"""
        if self.outbox_queue is None:
            return
        self.outbox_queue.put_nowait(
            SessionOutboxPayload(session_id="s1", messages=list(self.outbox_items))
        )

    async def activity(self) -> list[SessionActivity]:
        return list(self.activities)

    async def snapshot(self, session_id: str) -> SessionSnapshot | None:
        if session_id != "s1":
            return None
        tree: Tree[SessionNode] = Tree(self.nodes, root_current=self.root_current)
        return SessionSnapshot(
            session_id="s1",
            nodes=list(self.nodes),
            current=tree.chain(),
            root_current=self.root_current,
            created_at=1_700_000_000.0,
            updated_at=1_700_000_001.0,
            steps=1,
            message_count=len(self.nodes),
        )

    async def activate(self, session_id: str, node: int) -> None:
        """测试替身：沿用真实语义，把父节点的当前分支指过去。"""
        self.activation_calls.append((session_id, node))
        if session_id != "s1":
            return
        target = next((item for item in self.nodes if item.id == node), None)
        if target is None:
            return
        if target.parent == 0:
            self.root_current = node
            return
        parent = next(item for item in self.nodes if item.id == target.parent)
        parent.current = node

    async def delete_node(self, session_id: str, node: int) -> int:
        """测试替身：删掉节点与全部后代。"""
        self.delete_calls.append((session_id, node))
        tree: Tree[SessionNode] = Tree(self.nodes, root_current=self.root_current)
        doomed = set(tree.subtree(node))
        self.nodes = [item for item in self.nodes if item.id not in doomed]
        return len(doomed)

    async def prompt(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment] | None = None,
        *,
        parent: int | None = None,
        regenerate: bool = False,
    ) -> SessionReplyResponse:
        self.last_attachments = list(attachments or [])
        self.last_prompt = {
            "session_id": session_id,
            "text": text,
            "parent": parent,
            "regenerate": regenerate,
        }
        if self.stream_queue is not None:
            for payload in (
                SessionStreamPayload(
                    session_id=session_id,
                    kind="step_start",
                    step=1,
                ),
                SessionStreamPayload(
                    session_id=session_id,
                    kind="text",
                    step=1,
                    text="收到：",
                ),
                SessionStreamPayload(
                    session_id=session_id,
                    kind="text",
                    step=1,
                    text=text,
                ),
            ):
                self.stream_queue.put_nowait(payload)
            # 让「watch 泵」把这几帧推完再返回，测试才能断言帧的顺序与次数
            await asyncio.sleep(0.05)
        return SessionReplyResponse(
            session_id=session_id,
            request_id="req-1",
            text=f"收到：{text}",
            steps=1,
        )

    async def get_attachment(self, digest: str) -> tuple[str, bytes] | None:
        return self.blobs.get(digest)

    async def reset(self, session_id: str) -> bool:
        self.reset_calls.append(session_id)
        return True

    async def tools(self) -> CatalogQueryResponse:
        return CatalogQueryResponse(
            session_id="s1",
            request_id="req-catalog",
            version=3,
            tools=[ToolDefinition.create("echo", "回显输入")],
        )

    async def system(self) -> SystemView:
        return SystemView(
            available=True,
            supervisor_pid=123,
            generation="gen",
            updated_at=1_700_000_000.0,
            services=[
                ServiceView(
                    name="agent",
                    kind="agent",
                    status="Ready",
                    pid=42,
                )
            ],
        )

    @asynccontextmanager
    async def subscribe_activity(
        self,
    ) -> AsyncGenerator[asyncio.Queue[SessionActivityPayload]]:
        queue: asyncio.Queue[SessionActivityPayload] = asyncio.Queue()
        self.activity_queue = queue
        try:
            yield queue
        finally:
            self.activity_queue = None

    @asynccontextmanager
    async def subscribe(
        self, session_id: str
    ) -> AsyncGenerator[asyncio.Queue[SessionStreamPayload]]:
        del session_id
        queue: asyncio.Queue[SessionStreamPayload] = asyncio.Queue()
        if self.pending_stream is not None:
            queue.put_nowait(self.pending_stream)
            self.pending_stream = None
        self.stream_queue = queue
        try:
            yield queue
        finally:
            self.stream_queue = None
