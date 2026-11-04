"""session 服务：把会话持久化能力暴露为总线 RPC。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import time
from collections.abc import Iterable
from typing import Protocol

from blue_contracts_session import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendRequest,
    SessionAppendResponse,
    SessionAttachmentBlob,
    SessionAttachmentGetEvent,
    SessionAttachmentGetRequest,
    SessionAttachmentReplyEvent,
    SessionAttachmentResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetRequest,
    SessionGetResponse,
    SessionInfo,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionResetRequest,
    SessionResetResponse,
    SessionSnapshot,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateRequest,
    SessionStateResponse,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionUpdateRequest,
    SessionUpdateResponse,
    sha256_hex,
)
from infinity_bus import Event, EventBus, EventHandler
from pydantic import BaseModel

from blue.errors import BlueError
from blue.transport import BusRuntime
from blue_service_session.config import SessionConfig
from blue_service_session.db import (
    AttachmentRecord,
    SessionDatabase,
    SessionMeta,
    SessionNotFoundError,
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
    ) -> SessionMeta:
        """追加消息（可同时落库附件）。"""
        ...

    async def get_attachment(self, digest: str) -> AttachmentRecord | None:
        """按哈希取附件。"""
        ...

    async def update(
        self, session_id: str, steps: int | None, touch: bool
    ) -> SessionMeta:
        """更新元信息。"""
        ...

    async def reset(self, session_id: str) -> bool:
        """删除会话。"""
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
    ) -> SessionMeta:
        """追加消息；附件与消息在同一事务里落库。

        :raises SessionAttachmentError: 附件超限、base64 损坏或哈希不匹配
        """
        blobs = self._decode_attachments(list(attachments))
        return await self._db.append(
            session_id, payloads, now=time.time(), attachments=blobs
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

    async def state(self) -> list[SessionInfo]:
        """列出会话。"""
        return await self._db.state()

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


class SessionRpcHandler(EventHandler):
    """会话 RPC 事件入口。"""

    def __init__(self, backend: SessionBackend) -> None:
        super().__init__(
            subscriptions=[
                SessionGetEvent.name,
                SessionAppendEvent.name,
                SessionUpdateEvent.name,
                SessionResetEvent.name,
                SessionStateEvent.name,
                SessionAttachmentGetEvent.name,
            ],
            handle_timeout=15.0,
        )
        self._backend = backend

    async def handle(
        self,
        payload: BaseModel | None,
        bus_proxy: EventBus.Proxy,
        raw_event: Event,
    ) -> None:
        del raw_event
        if isinstance(payload, SessionGetRequest):
            await self._handle_get(payload, bus_proxy)
        elif isinstance(payload, SessionAppendRequest):
            await self._handle_append(payload, bus_proxy)
        elif isinstance(payload, SessionUpdateRequest):
            await self._handle_update(payload, bus_proxy)
        elif isinstance(payload, SessionResetRequest):
            await self._handle_reset(payload, bus_proxy)
        elif isinstance(payload, SessionStateRequest):
            await self._handle_state(payload, bus_proxy)
        elif isinstance(payload, SessionAttachmentGetRequest):
            await self._handle_attachment(payload, bus_proxy)

    async def _handle_get(
        self, payload: SessionGetRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        try:
            record = await self._backend.ensure(
                payload.session_id, payload.system_prompt
            )
            reply = SessionGetResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                session=_snapshot(record),
            )
        except SessionNotFoundError as error:
            reply = SessionGetResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=str(error),
            )
        await bus_proxy.publish(SessionGetReplyEvent.name, reply)

    async def _handle_append(
        self, payload: SessionAppendRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        try:
            meta = await self._backend.append(
                payload.session_id, payload.messages, payload.attachments
            )
            reply = SessionAppendResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                message_count=meta.message_count,
                updated_at=meta.updated_at,
            )
        except (SessionNotFoundError, SessionAttachmentError) as error:
            reply = SessionAppendResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=str(error),
            )
        await bus_proxy.publish(SessionAppendReplyEvent.name, reply)

    async def _handle_update(
        self, payload: SessionUpdateRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        try:
            meta = await self._backend.update(
                payload.session_id, payload.steps, payload.touch
            )
            reply = SessionUpdateResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                steps=meta.steps,
                message_count=meta.message_count,
                updated_at=meta.updated_at,
            )
        except SessionNotFoundError as error:
            reply = SessionUpdateResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=str(error),
            )
        await bus_proxy.publish(SessionUpdateReplyEvent.name, reply)

    async def _handle_reset(
        self, payload: SessionResetRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        try:
            await self._backend.reset(payload.session_id)
            reply = SessionResetResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
            )
        except Exception as error:
            logger.warning("重置会话失败：%s", error, exc_info=True)
            reply = SessionResetResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=str(error),
            )
        await bus_proxy.publish(SessionResetReplyEvent.name, reply)

    async def _handle_attachment(
        self, payload: SessionAttachmentGetRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        """读取附件：命中回 base64，未命中回失败。"""
        record = await self._backend.get_attachment(payload.hash)
        if record is None:
            reply = SessionAttachmentResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=f"附件不存在：{payload.hash}",
            )
        else:
            reply = SessionAttachmentResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                hash=record.hash,
                mime=record.mime,
                size=record.size,
                data=base64.b64encode(record.data).decode("ascii"),
            )
        await bus_proxy.publish(SessionAttachmentReplyEvent.name, reply)

    async def _handle_state(
        self, payload: SessionStateRequest, bus_proxy: EventBus.Proxy
    ) -> None:
        try:
            sessions = await self._backend.state()
            reply = SessionStateResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                sessions=sessions,
            )
        except Exception as error:
            logger.warning("读取会话状态失败：%s", error, exc_info=True)
            reply = SessionStateResponse(
                session_id=payload.session_id,
                request_id=payload.request_id,
                success=False,
                error_msg=str(error),
            )
        await bus_proxy.publish(SessionStateReplyEvent.name, reply)


def _snapshot(record: SessionRecord) -> SessionSnapshot:
    meta = record.meta
    return SessionSnapshot(
        session_id=meta.session_id,
        messages=record.messages,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        steps=meta.steps,
        message_count=meta.message_count,
    )
