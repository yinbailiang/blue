"""会话领域的事件契约。

本模块只依赖 ``pydantic`` / ``infinity_bus``，不依赖 ``blue`` 或任何服务实现。
"""

from __future__ import annotations

from typing import Any, Literal

from infinity_bus import EventDeclaration
from infinity_bus.templates.request import RequestProtocol, ResponseProtocol
from pydantic import BaseModel, Field, model_validator

__all__ = [
    "EVENTS",
    "SessionActivity",
    "SessionActivityEvent",
    "SessionActivityPayload",
    "SessionActivityQueryEvent",
    "SessionActivityQueryRequest",
    "SessionActivityQueryResponse",
    "SessionActivityReplyEvent",
    "SessionAppendEvent",
    "SessionAppendReplyEvent",
    "SessionAppendRequest",
    "SessionAppendResponse",
    "SessionAttachment",
    "SessionAttachmentBlob",
    "SessionAttachmentGetRequest",
    "SessionAttachmentResponse",
    "SessionCancelEvent",
    "SessionCancelReplyEvent",
    "SessionCancelRequest",
    "SessionCancelResponse",
    "SessionGetEvent",
    "SessionGetReplyEvent",
    "SessionGetRequest",
    "SessionGetResponse",
    "SessionInfo",
    "SessionPromptEvent",
    "SessionPromptRequest",
    "SessionReplyEvent",
    "SessionReplyResponse",
    "SessionResetEvent",
    "SessionResetReplyEvent",
    "SessionResetRequest",
    "SessionResetResponse",
    "SessionSnapshot",
    "SessionStateEvent",
    "SessionStateReplyEvent",
    "SessionStateRequest",
    "SessionStateResponse",
    "SessionStreamEvent",
    "SessionStreamPayload",
    "SessionUpdateEvent",
    "SessionUpdateReplyEvent",
    "SessionUpdateRequest",
    "SessionUpdateResponse",
    "StreamToolCall",
]


# ---------------------------------------------------------------------------
# 会话（RPC）
# ---------------------------------------------------------------------------


class SessionAttachment(BaseModel):
    """随 prompt 内联上传的附件（方案 A：一次往返）。"""

    mime: str = Field(description="声明的 MIME 类型，如 image/png")
    data: str = Field(description="base64 负载，可为裸 base64 或 data URL")
    name: str | None = Field(default=None, description="可选文件名")


class SessionAttachmentBlob(BaseModel):
    """落库用的附件负载：与消息一起内联提交，由会话库按哈希去重。"""

    hash: str = Field(description="内容哈希（sha256），与消息里的伪 URL 一致")
    mime: str = Field(description="MIME 类型")
    data: str = Field(description="base64 负载（不含 data: 前缀）")


class SessionPromptRequest(RequestProtocol):
    """用户向某个会话投递一条消息（可携带附件）。"""

    text: str = Field(default="", description="用户消息；与 attachments 至少给一个")
    attachments: list[SessionAttachment] = Field(
        default_factory=list[SessionAttachment], description="内联附件"
    )

    @model_validator(mode="after")
    def validate_content(self) -> SessionPromptRequest:
        """文本与附件不能同时为空。

        :raises ValueError: 两者都为空
        """
        if not self.text.strip() and not self.attachments:
            raise ValueError("text 与 attachments 至少要有一个")
        return self


class SessionReplyResponse(ResponseProtocol):
    """Agent 对一个会话消息的最终回复。"""

    text: str = Field(default="", description="回复文本")
    steps: int = Field(default=0, description="本次处理消耗的模型步数")


class SessionCancelRequest(RequestProtocol):
    """中断某个会话正在进行的回复。"""


class SessionCancelResponse(ResponseProtocol):
    """中断结果。"""

    cancelled: bool = Field(default=False, description="是否确有进行中的回复被中断")


class SessionResetRequest(RequestProtocol):
    """清空某个会话的历史。"""


class SessionResetResponse(ResponseProtocol):
    """会话重置结果。"""


class SessionInfo(BaseModel):
    """会话元信息（不含消息正文）。"""

    session_id: str = Field(description="会话 ID")
    message_count: int = Field(description="消息条数")
    updated_at: float = Field(description="最后更新时间（epoch 秒）")


class SessionSnapshot(BaseModel):
    """会话完整快照；消息以 JSON 文本传输，避免 Message 字段序列化歧义。"""

    session_id: str = Field(description="会话 ID")
    messages: list[str] = Field(
        default_factory=list[str], description="Message JSON 文本列表"
    )
    created_at: float = Field(description="创建时间（epoch 秒）")
    updated_at: float = Field(description="最后更新时间（epoch 秒）")
    steps: int = Field(default=0, description="最近一轮消耗的模型步数")
    message_count: int = Field(default=0, description="消息条数")


class StreamToolCall(BaseModel):
    """流式事件中的工具调用（避免 ToolCall.arguments 的序列化特例）。"""

    id: str = Field(description="工具调用 ID")
    name: str = Field(description="工具名")
    arguments: dict[str, Any] = Field(
        default_factory=dict[str, Any], description="工具参数"
    )


class SessionActivity(BaseModel):
    """一个会话的**运行状态**：谁在跑、跑到哪一步。

    Agent 是唯一的事实来源：它知道某个会话是否有正在进行的 turn。前端刷新、
    另一台设备连入时都靠这份状态恢复「正在生成」的追踪。
    """

    session_id: str = Field(description="会话 ID")
    running: bool = Field(description="是否有正在进行的 turn")
    phase: str = Field(
        default="",
        description="当前阶段：thinking（等模型）/ streaming（出字）/ tool（跑工具）",
    )
    step: int = Field(default=0, description="当前模型步骤序号")
    request_id: str = Field(default="", description="本轮请求 ID")
    started_at: float = Field(default=0.0, description="本轮开始时间（epoch 秒）")
    updated_at: float = Field(default=0.0, description="状态最后变化时间（epoch 秒）")


class SessionActivityPayload(BaseModel):
    """会话运行状态变化（广播）。"""

    activity: SessionActivity = Field(description="变化后的运行状态")


class SessionActivityEvent(EventDeclaration):
    """Agent 广播某个会话的运行状态变化。"""

    name = "session.activity"
    payload_type = SessionActivityPayload


class SessionActivityQueryRequest(RequestProtocol):
    """查询当前所有正在运行的会话（前端刚连上时用它补全状态）。"""


class SessionActivityQueryResponse(ResponseProtocol):
    """正在运行的会话快照。"""

    running: list[SessionActivity] = Field(
        default_factory=list[SessionActivity], description="当前进行中的会话"
    )


class SessionActivityQueryEvent(EventDeclaration):
    """查询正在运行的会话。"""

    name = "session.activity.query"
    payload_type = SessionActivityQueryRequest


class SessionActivityReplyEvent(EventDeclaration):
    """正在运行的会话快照回复。"""

    name = "session.activity.reply"
    payload_type = SessionActivityQueryResponse


class SessionStreamPayload(BaseModel):
    """Agent 实时活动通知。

    这是广播事件而非 RPC：WebUI 按 ``session_id`` 订阅，收到后即时推送到浏览器。
    ``kind`` 决定前端如何增量渲染。
    """

    session_id: str = Field(description="会话 ID")
    request_id: str = Field(default="", description="本轮请求 ID")
    kind: Literal[
        "queued",
        "step_start",
        "thinking",
        "text",
        "tool_calls",
        "tool_start",
        "tool_result",
        "done",
        "cancelled",
        "error",
    ] = Field(description="流式事件类型")
    step: int = Field(default=0, description="当前模型步骤序号")
    text: str = Field(default="", description="文本增量、工具结果或错误信息")
    tool_call: StreamToolCall | None = Field(
        default=None, description="单个工具调用（tool_start / tool_result）"
    )
    tool_calls: list[StreamToolCall] = Field(
        default_factory=list[StreamToolCall], description="模型返回的全部工具调用"
    )
    success: bool = Field(default=True, description="工具调用是否成功")
    attachments: list[str] = Field(
        default_factory=list[str],
        description="本次结果携带的附件哈希（供前端按 /api/attachments/<hash> 取图）",
    )


class SessionStreamEvent(EventDeclaration):
    """Agent 流式活动事件。"""

    name = "session.stream"
    payload_type = SessionStreamPayload


class SessionGetRequest(RequestProtocol):
    """取会话；不存在时按 system_prompt 创建。"""

    system_prompt: str = Field(default="", description="新会话的 system 消息")


class SessionGetResponse(ResponseProtocol):
    """会话快照。"""

    session: SessionSnapshot | None = Field(default=None, description="会话快照")


class SessionAppendRequest(RequestProtocol):
    """向会话追加消息（可同时提交消息引用的附件负载）。"""

    messages: list[str] = Field(
        default_factory=list[str], description="Message JSON 文本列表"
    )
    attachments: list[SessionAttachmentBlob] = Field(
        default_factory=list[SessionAttachmentBlob],
        description="消息里引用的附件负载（按哈希去重后落库）",
    )


class SessionAppendResponse(ResponseProtocol):
    """追加结果。"""

    message_count: int = Field(default=0, description="追加后的消息总数")
    updated_at: float = Field(default=0.0, description="最后更新时间")


class SessionUpdateRequest(RequestProtocol):
    """更新会话步数/活动时间。"""

    steps: int | None = Field(default=None, description="最近一轮模型步数")
    touch: bool = Field(default=True, description="是否刷新 updated_at")


class SessionUpdateResponse(ResponseProtocol):
    """更新结果。"""

    steps: int = Field(default=0, description="当前步数")
    message_count: int = Field(default=0, description="消息总数")
    updated_at: float = Field(default=0.0, description="最后更新时间")


class SessionGetEvent(EventDeclaration):
    """会话获取请求事件。"""

    name = "session.get"
    payload_type = SessionGetRequest


class SessionGetReplyEvent(EventDeclaration):
    """会话获取响应事件。"""

    name = "session.get.reply"
    payload_type = SessionGetResponse


class SessionAppendEvent(EventDeclaration):
    """会话追加请求事件。"""

    name = "session.append"
    payload_type = SessionAppendRequest


class SessionAppendReplyEvent(EventDeclaration):
    """会话追加响应事件。"""

    name = "session.append.reply"
    payload_type = SessionAppendResponse


class SessionUpdateEvent(EventDeclaration):
    """会话更新请求事件。"""

    name = "session.update"
    payload_type = SessionUpdateRequest


class SessionUpdateReplyEvent(EventDeclaration):
    """会话更新响应事件。"""

    name = "session.update.reply"
    payload_type = SessionUpdateResponse


class SessionStateRequest(RequestProtocol):
    """查询全部会话元信息。"""


class SessionStateResponse(ResponseProtocol):
    """会话元信息列表。"""

    sessions: list[SessionInfo] = Field(default_factory=list[SessionInfo])


class SessionPromptEvent(EventDeclaration):
    """会话消息请求事件。"""

    name = "session.prompt"
    payload_type = SessionPromptRequest


class SessionReplyEvent(EventDeclaration):
    """会话消息响应事件。"""

    name = "session.reply"
    payload_type = SessionReplyResponse


class SessionCancelEvent(EventDeclaration):
    """会话中断请求事件。"""

    name = "session.cancel"
    payload_type = SessionCancelRequest


class SessionCancelReplyEvent(EventDeclaration):
    """会话中断响应事件。"""

    name = "session.cancel.reply"
    payload_type = SessionCancelResponse


class SessionResetEvent(EventDeclaration):
    """会话重置请求事件。"""

    name = "session.reset"
    payload_type = SessionResetRequest


class SessionResetReplyEvent(EventDeclaration):
    """会话重置响应事件。"""

    name = "session.reset.reply"
    payload_type = SessionResetResponse


class SessionStateEvent(EventDeclaration):
    """会话状态查询请求事件。"""

    name = "session.state"
    payload_type = SessionStateRequest


class SessionStateReplyEvent(EventDeclaration):
    """会话状态查询响应事件。"""

    name = "session.state.reply"
    payload_type = SessionStateResponse


class SessionAttachmentGetRequest(RequestProtocol):
    """按内容哈希取附件。"""

    hash: str = Field(description="附件内容哈希")


class SessionAttachmentResponse(ResponseProtocol):
    """附件内容（base64）与元信息。"""

    hash: str = Field(default="", description="附件内容哈希")
    mime: str = Field(default="", description="MIME 类型")
    size: int = Field(default=0, description="原始字节数")
    data: str = Field(default="", description="base64 负载（不含 data: 前缀）")


class SessionAttachmentGetEvent(EventDeclaration):
    """附件读取请求事件。"""

    name = "session.attachment.get"
    payload_type = SessionAttachmentGetRequest


class SessionAttachmentReplyEvent(EventDeclaration):
    """附件读取响应事件。"""

    name = "session.attachment.reply"
    payload_type = SessionAttachmentResponse


EVENTS: tuple[type[EventDeclaration], ...] = (
    SessionPromptEvent,
    SessionReplyEvent,
    SessionCancelEvent,
    SessionCancelReplyEvent,
    SessionResetEvent,
    SessionResetReplyEvent,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionUpdateEvent,
    SessionUpdateReplyEvent,
    SessionStreamEvent,
    SessionActivityEvent,
    SessionActivityQueryEvent,
    SessionActivityReplyEvent,
    SessionAttachmentGetEvent,
    SessionAttachmentReplyEvent,
)
"""会话领域全部跨进程事件声明。"""
