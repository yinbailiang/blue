"""session 服务：把会话持久化能力暴露为总线 RPC。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import time
from collections.abc import Iterable
from typing import Any, Protocol, cast

from blue_contracts_session import (
    QueuedMessage,
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendRequest,
    SessionAttachment,
    SessionAttachmentBlob,
    SessionAttachmentGetEvent,
    SessionAttachmentGetRequest,
    SessionAttachmentReplyEvent,
    SessionBranchActivateEvent,
    SessionBranchActivateReplyEvent,
    SessionBranchActivateRequest,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetRequest,
    SessionInfo,
    SessionNode,
    SessionNodeDeleteEvent,
    SessionNodeDeleteReplyEvent,
    SessionNodeDeleteRequest,
    SessionNodeLabelEvent,
    SessionNodeLabelReplyEvent,
    SessionNodeLabelRequest,
    SessionOutboxClaimEvent,
    SessionOutboxClaimReplyEvent,
    SessionOutboxClaimRequest,
    SessionOutboxClearEvent,
    SessionOutboxClearReplyEvent,
    SessionOutboxClearRequest,
    SessionOutboxCompleteEvent,
    SessionOutboxCompleteReplyEvent,
    SessionOutboxCompleteRequest,
    SessionOutboxEnqueueEvent,
    SessionOutboxEnqueueReplyEvent,
    SessionOutboxEnqueueRequest,
    SessionOutboxEvent,
    SessionOutboxFailEvent,
    SessionOutboxFailReplyEvent,
    SessionOutboxFailRequest,
    SessionOutboxListEvent,
    SessionOutboxListReplyEvent,
    SessionOutboxListRequest,
    SessionOutboxMoveEvent,
    SessionOutboxMoveReplyEvent,
    SessionOutboxMoveRequest,
    SessionOutboxPayload,
    SessionOutboxRemoveEvent,
    SessionOutboxRemoveReplyEvent,
    SessionOutboxRemoveRequest,
    SessionOutboxRetryEvent,
    SessionOutboxRetryReplyEvent,
    SessionOutboxRetryRequest,
    SessionOutboxSendNowEvent,
    SessionOutboxSendNowReplyEvent,
    SessionOutboxSendNowRequest,
    SessionOutboxUpdateEvent,
    SessionOutboxUpdateReplyEvent,
    SessionOutboxUpdateRequest,
    SessionReadEvent,
    SessionReadMessage,
    SessionReadReplyEvent,
    SessionReadRequest,
    SessionRenameEvent,
    SessionRenameReplyEvent,
    SessionRenameRequest,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetRequest,
    SessionSearchEvent,
    SessionSearchHit,
    SessionSearchReplyEvent,
    SessionSearchRequest,
    SessionSnapshot,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateRequest,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateRequest,
    sha256_hex,
)
from infinity_bus import Event, EventBus
from infinity_bus.templates import (
    ConflictError,
    NotFoundError,
    ServiceHandler,
    process,
)
from infinity_bus.templates.request import (
    RequestProtocol,
    ResponseProtocol,
    build_response,
)
from pydantic import BaseModel

from blue.errors import BlueError
from blue.transport import BusRuntime
from blue.tree import Tree
from blue_service_session.config import SessionConfig
from blue_service_session.db import (
    AppendResult,
    AttachmentRecord,
    SessionDatabase,
    SessionMeta,
    SessionNodeRecord,
    SessionRecord,
)

logger = logging.getLogger(__name__)

__all__ = ["SessionBackend", "SessionRpcHandler", "SessionService"]


class SessionAttachmentError(BlueError):
    """附件不合法（过大、超数量、哈希不匹配、base64 损坏）。"""


class SessionBackend(Protocol):
    """RPC 处理器依赖的会话存储能力。"""

    async def ensure(self, session_id: str, system_prompt: str) -> SessionRecord:
        """取/建会话。"""
        ...

    async def append(
        self,
        session_id: str,
        payloads: list[str],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> AppendResult:
        """追加消息（可同时落库附件）。"""
        ...

    async def get_attachment(self, digest: str) -> AttachmentRecord | None:
        """按哈希取附件。"""
        ...

    async def enqueue_message(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment],
        *,
        now: float,
    ) -> QueuedMessage:
        """把一条消息放进待发送队列。"""
        ...

    async def list_outbox(self, session_id: str) -> list[QueuedMessage]:
        """按入队顺序列出待发送消息。"""
        ...

    async def remove_message(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息。"""
        ...

    async def complete_outbox(self, message_id: str) -> bool:
        """认领的队列项已跑完：从队列删除。"""
        ...

    async def fail_outbox(self, message_id: str, error: str) -> bool:
        """认领的队列项失败了：留错误原因。"""
        ...

    async def retry_outbox(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发。"""
        ...

    async def update_outbox(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment],
    ) -> bool:
        """改队列项的正文/附件。"""
        ...

    async def move_outbox(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序。"""
        ...

    async def clear_outbox(self, session_id: str) -> int:
        """清空队列（保留发送中的那条）。"""
        ...

    async def outbox_depth(self, session_id: str) -> int:
        """还有几条待发（pending + sending）。"""
        ...

    async def claim_message(
        self, session_id: str, *, now: float
    ) -> QueuedMessage | None:
        """取出队首并从队列移除。"""
        ...

    async def update(
        self, session_id: str, steps: int | None, touch: bool
    ) -> SessionMeta:
        """更新元信息。"""
        ...

    async def reset(self, session_id: str) -> bool:
        """删除会话。"""
        ...

    async def set_current(self, session_id: str, node_id: int) -> SessionMeta:
        """切换当前分支。"""
        ...

    async def set_title(self, session_id: str, title: str) -> str:
        """设置会话标题；空字符串表示恢复自动标题。"""
        ...

    async def delete_node(self, session_id: str, node_id: int) -> int:
        """删除节点及其后代，返回删除条数。"""
        ...

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 5,
        roles: Iterable[str] = ("user", "assistant", "tool"),
    ) -> list[tuple[int, str, int, int, str]]:
        """在当前链上按子串搜索历史。"""
        ...

    async def read(
        self, session_id: str, ids: Iterable[int], *, max_chars: int = 4000
    ) -> tuple[list[tuple[int, str, int, str, bool]], list[int]]:
        """按 id 读原文。"""
        ...

    async def set_label(self, session_id: str, node_id: int, label: str) -> str:
        """给分支起名；空串恢复序号显示。"""
        ...

    async def state(self) -> list[SessionInfo]:
        """列出会话。"""
        ...


class SessionService:
    """会话数据库 + 总线成员。"""

    def __init__(self, *, config: SessionConfig, runtime: BusRuntime) -> None:
        self._config = config
        self._runtime = runtime
        self._db = SessionDatabase(
            config.resolve_database_path(),
            max_sessions=config.max_sessions,
            idle_ttl=config.idle_ttl,
            busy_timeout_ms=config.busy_timeout_ms,
            active_grace=config.active_grace,
            prune_batch_size=config.prune_batch_size,
            enforce_capacity=config.prune_enabled,
        )
        self._pruner: asyncio.Task[None] | None = None

    @property
    def database(self) -> SessionDatabase:
        """会话数据库。"""
        return self._db

    async def start(self) -> None:
        """连接数据库、启动总线、注册 RPC 处理器与过期回收任务。"""
        await self._db.connect()
        try:
            await self._runtime.start()
            await self._runtime.register(SessionRpcHandler(self))
        except Exception:
            await self._runtime.stop()
            await self._db.close()
            raise
        if self._config.prune_enabled:
            self._pruner = asyncio.create_task(
                self._prune_loop(), name="session.pruner"
            )
            logger.info(
                "session 服务已启动（%s）；自动回收已开启：上限 %s 个会话、"
                "空闲 %s 秒回收",
                self._db.path,
                self._config.max_sessions,
                self._config.idle_ttl,
            )
        else:
            logger.info(
                "session 服务已启动（%s）；自动回收已关闭：不会自动删除会话或附件",
                self._db.path,
            )

    async def stop(self) -> None:
        """停止回收任务、总线与数据库。"""
        pruner, self._pruner = self._pruner, None
        if pruner is not None:
            pruner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pruner
        await self._runtime.stop()
        await self._db.close()

    async def ensure(self, session_id: str, system_prompt: str) -> SessionRecord:
        """取/建会话。"""
        return await self._db.ensure(session_id, system_prompt, now=time.time())

    async def append(
        self,
        session_id: str,
        payloads: list[str],
        attachments: Iterable[SessionAttachmentBlob] = (),
        parent: int | None = None,
    ) -> AppendResult:
        """追加消息；附件与消息在同一事务里落库。

        :raises SessionAttachmentError: 附件超限、base64 损坏或哈希不匹配
        """
        blobs = self._decode_attachments(list(attachments))
        return await self._db.append(
            session_id, payloads, now=time.time(), attachments=blobs, parent=parent
        )

    async def get_attachment(self, digest: str) -> AttachmentRecord | None:
        """按内容哈希取附件。"""
        return await self._db.get_attachment(digest)

    def _decode_attachments(
        self, blobs: list[SessionAttachmentBlob]
    ) -> list[tuple[str, str, bytes]]:
        """校验并解码附件负载。

        :raises SessionAttachmentError: 数量超限、单条过大、base64 损坏或哈希不匹配
        """
        limit = self._config.max_attachments_per_message
        if len(blobs) > limit:
            raise SessionAttachmentError(f"附件数量超过上限（{len(blobs)} > {limit}）")
        decoded: list[tuple[str, str, bytes]] = []
        for blob in blobs:
            try:
                data = base64.b64decode(blob.data, validate=True)
            except (binascii.Error, ValueError) as error:
                raise SessionAttachmentError(f"附件 base64 非法：{error}") from error
            if len(data) > self._config.max_attachment_bytes:
                limit_bytes = self._config.max_attachment_bytes
                raise SessionAttachmentError(
                    f"附件过大（{len(data)} > {limit_bytes} 字节）"
                )
            if sha256_hex(data) != blob.hash:
                raise SessionAttachmentError("附件哈希与内容不一致")
            decoded.append((blob.hash, blob.mime, data))
        return decoded

    async def update(
        self, session_id: str, steps: int | None, touch: bool
    ) -> SessionMeta:
        """更新会话步数/活动时间。"""
        return await self._db.update(
            session_id, steps=steps, touch=touch, now=time.time()
        )

    async def reset(self, session_id: str) -> bool:
        """删除会话。"""
        return await self._db.reset(session_id)

    async def set_title(self, session_id: str, title: str) -> str:
        """设置会话标题；空字符串表示恢复自动标题。"""
        return await self._db.set_title(session_id, title)

    async def set_current(self, session_id: str, node_id: int) -> SessionMeta:
        """切换当前分支。"""
        return await self._db.set_current(session_id, node_id)

    async def delete_node(self, session_id: str, node_id: int) -> int:
        """删除一条分支（节点 + 后代）。"""
        return await self._db.delete_node(session_id, node_id, now=time.time())

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 5,
        roles: Iterable[str] = ("user", "assistant", "tool"),
    ) -> list[tuple[int, str, int, int, str]]:
        """在当前链上按子串搜索历史。"""
        return await self._db.search(session_id, query, limit=limit, roles=roles)

    async def read(
        self, session_id: str, ids: Iterable[int], *, max_chars: int = 4000
    ) -> tuple[list[tuple[int, str, int, str, bool]], list[int]]:
        """按 id 读原文。"""
        return await self._db.read(session_id, ids, max_chars=max_chars)

    async def set_label(self, session_id: str, node_id: int, label: str) -> str:
        """给分支起名；空串恢复序号显示。"""
        return await self._db.set_label(session_id, node_id, label)

    async def state(self) -> list[SessionInfo]:
        """列出会话。"""
        return await self._db.state()

    async def enqueue_message(
        self,
        session_id: str,
        text: str,
        attachments: list[SessionAttachment],
        *,
        now: float,
    ) -> QueuedMessage:
        """把一条消息放进待发送队列。"""
        return await self._db.enqueue_message(session_id, text, attachments, now=now)

    async def list_outbox(self, session_id: str) -> list[QueuedMessage]:
        """按入队顺序列出待发送消息。"""
        return await self._db.list_outbox(session_id)

    async def remove_message(self, session_id: str, message_id: str) -> bool:
        """取消一条排队消息。"""
        return await self._db.remove_message(session_id, message_id)

    async def complete_outbox(self, message_id: str) -> bool:
        """认领的队列项已跑完：从队列删除。"""
        return await self._db.complete_message(message_id)

    async def fail_outbox(self, message_id: str, error: str) -> bool:
        """认领的队列项失败了：留错误原因。"""
        return await self._db.fail_message(message_id, error)

    async def retry_outbox(self, session_id: str, message_id: str) -> bool:
        """把失败项放回待发。"""
        return await self._db.retry_message(session_id, message_id)

    async def update_outbox(
        self,
        session_id: str,
        message_id: str,
        text: str,
        attachments: list[SessionAttachment],
    ) -> bool:
        """改队列项的正文/附件。"""
        return await self._db.update_message(session_id, message_id, text, attachments)

    async def move_outbox(self, session_id: str, message_id: str, to: str) -> bool:
        """调整排队顺序。"""
        return await self._db.move_message(session_id, message_id, to)

    async def clear_outbox(self, session_id: str) -> int:
        """清空队列（保留发送中的那条）。"""
        return await self._db.clear_outbox(session_id)

    async def outbox_depth(self, session_id: str) -> int:
        """还有几条待发（pending + sending）。"""
        return await self._db.outbox_depth(session_id)

    async def claim_message(
        self, session_id: str, *, now: float
    ) -> QueuedMessage | None:
        """认领队首（pending → sending），超时未完成的自动回收。"""
        return await self._db.claim_message(
            session_id, now=now, stale_after=self._config.outbox_claim_timeout
        )

    async def _prune_loop(self) -> None:
        """定期回收旧会话与无引用附件；启动时先做一轮，重启后立刻见效。"""
        interval = self._config.prune_interval
        while True:
            try:
                await self.reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("会话回收失败", exc_info=True)
            await asyncio.sleep(interval)

    async def reap_once(self) -> None:
        """执行一轮回收（会话 + 附件），结果写日志。

        只有在 ``prune_enabled`` 打开时才会删除会话；关闭时这里什么都不做，
        因此也可以安全地被外部（测试/运维）调用。
        """
        if not self._config.prune_enabled:
            logger.debug("自动回收已关闭，跳过本轮回收")
            return
        now = time.time()
        result = await self._db.prune(now=now)
        if result.total:
            sample = ", ".join(result.removed[:10])
            if result.total > 10:
                sample += f" …（共 {result.total} 个）"
            logger.info(
                "已回收会话 %s 个（过期 %s / 超限淘汰 %s）：%s",
                result.total,
                len(result.expired),
                len(result.evicted),
                sample,
            )
        swept = await self._db.sweep_attachments(
            now=now, grace=self._config.attachment_grace
        )
        if swept:
            logger.info("已回收无引用附件 %s 个", swept)


class _SessionRpcBase(ServiceHandler):
    """会话 RPC 基类：把 blue 的业务错误统一转成 ``success=False`` 的应答。

    ``ServiceHandler`` 只认 ``ServiceError``；会话服务抛的是 ``BlueError`` 家族
    （含 ``TreeError``），所以这里统一兜一层。顺便把任何意外异常也转成失败应答——
    否则发起方会等到超时，比拿到错误信息更糟。
    """

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        try:
            await super().handle(payload, bus_proxy, raw_event)
        except Exception as error:
            meta = self.processors.get(raw_event.name)
            if meta is None or meta.response is None:
                raise
            logger.warning(
                "%s 处理失败：%s",
                raw_event.name,
                error,
                exc_info=not isinstance(error, BlueError),
            )
            assert payload is not None
            assert meta.response.payload_type is not None
            request = cast("RequestProtocol", payload)
            response_type = cast("type[ResponseProtocol]", meta.response.payload_type)
            await bus_proxy.publish(
                meta.response.name,
                build_response(
                    request, response_type, success=False, error_msg=str(error)
                ),
            )


class SessionRpcHandler(_SessionRpcBase):
    """会话 RPC 事件入口。

    订阅列表由 ``@process`` 自动生成——不会再出现「加了事件却忘了写进
    ``subscriptions``」那种只能靠往返测试才发现的问题。
    """

    def __init__(self, backend: SessionBackend) -> None:
        self._backend = backend
        super().__init__()

    # -- 会话读写 ------------------------------------------------------

    @process(SessionGetEvent, SessionGetReplyEvent)
    async def on_get(
        self, payload: SessionGetRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """取会话快照（不存在则建）。"""
        del bus_proxy, raw_event
        record = await self._backend.ensure(payload.session_id, payload.system_prompt)
        return {"session": _snapshot(record)}

    @process(SessionAppendEvent, SessionAppendReplyEvent)
    async def on_append(
        self, payload: SessionAppendRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """追加一条链；一次追加里的消息自动串成父子。"""
        del bus_proxy, raw_event
        result = await self._backend.append(
            payload.session_id,
            payload.messages,
            payload.attachments,
            payload.parent,
        )
        return {
            "message_count": result.meta.message_count,
            "updated_at": result.meta.updated_at,
            "nodes": result.nodes,
        }

    @process(SessionUpdateEvent, SessionUpdateReplyEvent)
    async def on_update(
        self, payload: SessionUpdateRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """更新步数与时间戳。"""
        del bus_proxy, raw_event
        meta = await self._backend.update(
            payload.session_id, payload.steps, payload.touch
        )
        return {
            "steps": meta.steps,
            "message_count": meta.message_count,
            "updated_at": meta.updated_at,
        }

    @process(SessionStateEvent, SessionStateReplyEvent)
    async def on_state(
        self, payload: SessionStateRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """列出会话。"""
        del bus_proxy, raw_event
        return {"sessions": await self._backend.state()}

    @process(SessionResetEvent, SessionResetReplyEvent)
    async def on_reset(
        self, payload: SessionResetRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """删除会话。"""
        del bus_proxy, raw_event
        await self._backend.reset(payload.session_id)
        return {}

    @process(SessionRenameEvent, SessionRenameReplyEvent)
    async def on_rename(
        self, payload: SessionRenameRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """改名；空标题恢复自动标题。"""
        del bus_proxy, raw_event
        return {
            "title": await self._backend.set_title(payload.session_id, payload.title)
        }

    # -- 对话树 --------------------------------------------------------

    @process(SessionBranchActivateEvent, SessionBranchActivateReplyEvent)
    async def on_branch_activate(
        self,
        payload: SessionBranchActivateRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """切换当前分支：只改指针，不动任何消息。"""
        del bus_proxy, raw_event
        await self._backend.set_current(payload.session_id, payload.node)
        return {}

    @process(SessionNodeDeleteEvent, SessionNodeDeleteReplyEvent)
    async def on_node_delete(
        self,
        payload: SessionNodeDeleteRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """删除一条分支（节点与其后代）。"""
        del bus_proxy, raw_event
        removed = await self._backend.delete_node(payload.session_id, payload.node)
        return {"removed": removed}

    @process(SessionNodeLabelEvent, SessionNodeLabelReplyEvent)
    async def on_node_label(
        self,
        payload: SessionNodeLabelRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """给分支起名；空串恢复序号显示。"""
        del raw_event
        label = await self._backend.set_label(
            payload.session_id, payload.node, payload.label
        )
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {"label": label}

    # -- 历史回取 ------------------------------------------------------

    @process(SessionSearchEvent, SessionSearchReplyEvent)
    async def on_search(
        self, payload: SessionSearchRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """在当前链上按子串搜索历史。"""
        del bus_proxy, raw_event
        roles = [role.strip() for role in payload.roles.split(",") if role.strip()]
        hits = await self._backend.search(
            payload.session_id, payload.query, limit=payload.limit, roles=roles
        )
        return {
            "total": len(hits),
            "hits": [
                SessionSearchHit(
                    id=node_id, role=role, index=index, chars=chars, snippet=snippet
                )
                for node_id, role, index, chars, snippet in hits
            ],
        }

    @process(SessionReadEvent, SessionReadReplyEvent)
    async def on_read(
        self, payload: SessionReadRequest, bus_proxy: EventBus.Proxy, raw_event: Event
    ) -> dict[str, Any]:
        """按 id 读原文；不存在的 id 放进 ``missing``。"""
        del bus_proxy, raw_event
        messages, missing = await self._backend.read(
            payload.session_id, payload.ids, max_chars=payload.max_chars
        )
        return {
            "messages": [
                SessionReadMessage(
                    id=node_id,
                    role=role,
                    chars=chars,
                    content=content,
                    truncated=truncated,
                )
                for node_id, role, chars, content, truncated in messages
            ],
            "missing": missing,
        }

    # -- 附件 ----------------------------------------------------------

    @process(SessionAttachmentGetEvent, SessionAttachmentReplyEvent)
    async def on_attachment(
        self,
        payload: SessionAttachmentGetRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """读取附件：命中回 base64，未命中按 NotFound 回失败。"""
        del bus_proxy, raw_event
        record = await self._backend.get_attachment(payload.hash)
        if record is None:
            raise NotFoundError(f"附件不存在：{payload.hash}")
        return {
            "hash": record.hash,
            "mime": record.mime,
            "size": record.size,
            "data": base64.b64encode(record.data).decode("ascii"),
        }

    # -- 发件箱 --------------------------------------------------------

    @process(SessionOutboxEnqueueEvent, SessionOutboxEnqueueReplyEvent)
    async def on_outbox_enqueue(
        self,
        payload: SessionOutboxEnqueueRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """入队一条待发送消息。"""
        del raw_event
        message = await self._backend.enqueue_message(
            payload.session_id, payload.text, payload.attachments, now=time.time()
        )
        pending = await self._backend.list_outbox(payload.session_id)
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {"queued": message, "position": len(pending)}

    @process(SessionOutboxListEvent, SessionOutboxListReplyEvent)
    async def on_outbox_list(
        self,
        payload: SessionOutboxListRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """列出队列。"""
        del bus_proxy, raw_event
        return {"messages": await self._backend.list_outbox(payload.session_id)}

    @process(SessionOutboxRemoveEvent, SessionOutboxRemoveReplyEvent)
    async def on_outbox_remove(
        self,
        payload: SessionOutboxRemoveRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """取消一条排队消息。"""
        del raw_event
        removed = await self._backend.remove_message(
            payload.session_id, payload.message_id
        )
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {"removed": removed}

    @process(SessionOutboxUpdateEvent, SessionOutboxUpdateReplyEvent)
    async def on_outbox_update(
        self,
        payload: SessionOutboxUpdateRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """改排队消息的正文/附件（发送中的那条拒绝）。"""
        del raw_event
        changed = await self._backend.update_outbox(
            payload.session_id, payload.message_id, payload.text, payload.attachments
        )
        if not changed:
            raise ConflictError("这条消息正在发送，不能修改")
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}

    @process(SessionOutboxMoveEvent, SessionOutboxMoveReplyEvent)
    async def on_outbox_move(
        self,
        payload: SessionOutboxMoveRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """调整排队顺序：置顶 / 上移 / 下移。"""
        del raw_event
        changed = await self._backend.move_outbox(
            payload.session_id, payload.message_id, payload.to
        )
        if not changed:
            raise NotFoundError("队列项不存在")
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}

    @process(SessionOutboxSendNowEvent, SessionOutboxSendNowReplyEvent)
    async def on_outbox_send_now(
        self,
        payload: SessionOutboxSendNowRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """立刻发送 = 置顶 + 广播；agent 订阅广播后拉起队列泵。"""
        del raw_event
        changed = await self._backend.move_outbox(
            payload.session_id, payload.message_id, "top"
        )
        if not changed:
            raise NotFoundError("队列项不存在")
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}

    @process(SessionOutboxRetryEvent, SessionOutboxRetryReplyEvent)
    async def on_outbox_retry(
        self,
        payload: SessionOutboxRetryRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """把失败项放回待发。"""
        del raw_event
        changed = await self._backend.retry_outbox(
            payload.session_id, payload.message_id
        )
        if not changed:
            raise ConflictError("这条消息不在失败状态")
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}

    @process(SessionOutboxClearEvent, SessionOutboxClearReplyEvent)
    async def on_outbox_clear(
        self,
        payload: SessionOutboxClearRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """清空队列（正在发送的那条保留）。"""
        del raw_event
        removed = await self._backend.clear_outbox(payload.session_id)
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {"removed": removed}

    @process(SessionOutboxClaimEvent, SessionOutboxClaimReplyEvent)
    async def on_outbox_claim(
        self,
        payload: SessionOutboxClaimRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """认领队首（agent 队列泵用）。"""
        del bus_proxy, raw_event
        message = await self._backend.claim_message(payload.session_id, now=time.time())
        depth = await self._backend.outbox_depth(payload.session_id)
        return {"message": message, "remaining": depth}

    @process(SessionOutboxCompleteEvent, SessionOutboxCompleteReplyEvent)
    async def on_outbox_complete(
        self,
        payload: SessionOutboxCompleteRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """认领的队列项跑完了：删掉这一行。"""
        del raw_event
        await self._backend.complete_outbox(payload.message_id)
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}

    @process(SessionOutboxFailEvent, SessionOutboxFailReplyEvent)
    async def on_outbox_fail(
        self,
        payload: SessionOutboxFailRequest,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> dict[str, Any]:
        """认领的队列项失败了：留错误原因，等自动/人工重试。"""
        del raw_event
        await self._backend.fail_outbox(payload.message_id, payload.error)
        await _publish_outbox(self._backend, payload.session_id, bus_proxy)
        return {}


async def _publish_outbox(
    backend: SessionBackend, session_id: str, bus_proxy: EventBus.Proxy
) -> None:
    """广播一次队列快照（失败不影响主流程）。"""
    try:
        messages = await backend.list_outbox(session_id)
        await bus_proxy.publish(
            SessionOutboxEvent.name,
            SessionOutboxPayload(session_id=session_id, messages=messages),
        )
    except Exception:
        logger.warning("会话 %s 队列广播失败", session_id, exc_info=True)


def _snapshot(record: SessionRecord) -> SessionSnapshot:
    """把数据库记录转成线上快照：整棵树 + 当前链。"""
    meta = record.meta
    tree: Tree[SessionNodeRecord] = Tree(record.nodes, root_current=meta.root_current)
    return SessionSnapshot(
        session_id=meta.session_id,
        title=meta.title,
        nodes=[
            SessionNode(
                id=node.id,
                parent=node.parent,
                current=node.current,
                message=node.message,
                created_at=node.created_at,
                label=node.label,
            )
            for node in record.nodes
        ],
        current=tree.chain(),
        root_current=meta.root_current,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        steps=meta.steps,
        message_count=meta.message_count,
    )
