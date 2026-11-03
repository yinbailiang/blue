"""总线协议：**固定**的跨进程事件名与负载模型。

事件名是稳定常量（如 ``tool.call``），不随配置变化——运维查看 broker、抓包、
写告警时可以直接引用；代码里通过事件声明类的 ``name`` 使用，可被 IDE 跳转与
``grep`` 直接定位。

多目标/多实例靠**载荷字段**区分，接收方自行过滤：

- ``tool.call`` → ``ToolCallRequest.tool``（工具进程只响应对应工具名）；
- RPC 请求/响应 → ``session_id`` + ``request_id``（``infinity_bus`` 自动注入并匹配）；
- 工具进程生命周期 → ``worker_id``。

若需要在一套 broker 上隔离多套部署，使用 RabbitMQ 的 vhost / 用户隔离，
不要把隔离维度编码进事件名。
"""

from __future__ import annotations

from typing import Any, Literal

from infinity_agent import ToolDefinition
from infinity_bus import EventDeclaration, EventRegistry
from infinity_bus.templates.request import RequestProtocol, ResponseProtocol
from pydantic import BaseModel, Field

__all__ = [
    "ALL_EVENTS",
    "EVENT_NAMES",
    "CatalogChangedEvent",
    "CatalogChangedPayload",
    "CatalogQueryEvent",
    "CatalogQueryRequest",
    "CatalogQueryResponse",
    "CatalogReplyEvent",
    "SessionAppendEvent",
    "SessionAppendReplyEvent",
    "SessionAppendRequest",
    "SessionAppendResponse",
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
    "ToolCallEvent",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolHeartbeatEvent",
    "ToolHeartbeatPayload",
    "ToolRegisterEvent",
    "ToolRegisterPayload",
    "ToolResultEvent",
    "ToolUnregisterEvent",
    "ToolUnregisterPayload",
    "register_events",
]


# ---------------------------------------------------------------------------
# 工具注册中心
# ---------------------------------------------------------------------------


class ToolRegisterPayload(BaseModel):
    """工具进程上线：把自己登记到注册中心。"""

    worker_id: str = Field(description="工具进程唯一标识")
    tool: ToolDefinition = Field(description="工具定义（由函数签名自动推断）")
    ttl: float = Field(description="租约时长（秒），超时未续约即被清除")


class ToolHeartbeatPayload(BaseModel):
    """工具进程续约。"""

    worker_id: str = Field(description="工具进程唯一标识")


class ToolUnregisterPayload(BaseModel):
    """工具进程主动下线。"""

    worker_id: str = Field(description="工具进程唯一标识")


class CatalogChangedPayload(BaseModel):
    """工具目录变更广播。"""

    version: int = Field(description="目录版本号，单调递增")
    tools: list[ToolDefinition] = Field(
        default_factory=list[ToolDefinition], description="当前可用工具定义"
    )


class ToolRegisterEvent(EventDeclaration):
    """工具进程自我登记。"""

    name = "tool.register"
    payload_type = ToolRegisterPayload


class ToolHeartbeatEvent(EventDeclaration):
    """工具进程续约。"""

    name = "tool.heartbeat"
    payload_type = ToolHeartbeatPayload


class ToolUnregisterEvent(EventDeclaration):
    """工具进程主动下线。"""

    name = "tool.unregister"
    payload_type = ToolUnregisterPayload


class CatalogChangedEvent(EventDeclaration):
    """工具目录变更广播。"""

    name = "catalog.changed"
    payload_type = CatalogChangedPayload


# ---------------------------------------------------------------------------
# 工具调用（RPC）
# ---------------------------------------------------------------------------


class ToolCallRequest(RequestProtocol):
    """Agent 发起的工具调用请求。

    ``tool`` 即目标：扇出到所有工具进程后，只有同名进程执行，其余直接忽略。
    """

    tool: str = Field(description="目标工具名")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")


class ToolCallResult(ResponseProtocol):
    """工具进程返回的调用结果。"""

    content: str = Field(default="", description="工具输出（字符串或 JSON 文本）")


class ToolCallEvent(EventDeclaration):
    """工具调用请求事件。"""

    name = "tool.call"
    payload_type = ToolCallRequest


class ToolResultEvent(EventDeclaration):
    """工具调用结果事件。"""

    name = "tool.result"
    payload_type = ToolCallResult


# ---------------------------------------------------------------------------
# 工具目录查询（RPC）
# ---------------------------------------------------------------------------


class CatalogQueryRequest(RequestProtocol):
    """查询当前工具目录。"""


class CatalogQueryResponse(ResponseProtocol):
    """工具目录快照。"""

    version: int = Field(default=0, description="目录版本号")
    tools: list[ToolDefinition] = Field(
        default_factory=list[ToolDefinition], description="可用工具定义"
    )


class CatalogQueryEvent(EventDeclaration):
    """目录查询请求事件。"""

    name = "catalog.query"
    payload_type = CatalogQueryRequest


class CatalogReplyEvent(EventDeclaration):
    """目录查询响应事件。"""

    name = "catalog.reply"
    payload_type = CatalogQueryResponse


# ---------------------------------------------------------------------------
# 会话（RPC）
# ---------------------------------------------------------------------------


class SessionPromptRequest(RequestProtocol):
    """用户向某个会话投递一条消息。"""

    text: str = Field(description="用户消息")


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


class SessionStreamPayload(BaseModel):
    """Agent 实时活动通知。

    这是广播事件而非 RPC：WebUI 按 ``session_id`` 订阅，收到后即时推送到浏览器。
    ``kind`` 决定前端如何增量渲染。
    """

    session_id: str = Field(description="会话 ID")
    request_id: str = Field(default="", description="本轮请求 ID")
    kind: Literal[
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
    """向会话追加消息。"""

    messages: list[str] = Field(
        default_factory=list[str], description="Message JSON 文本列表"
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


ALL_EVENTS: tuple[type[EventDeclaration], ...] = (
    ToolRegisterEvent,
    ToolHeartbeatEvent,
    ToolUnregisterEvent,
    CatalogChangedEvent,
    ToolCallEvent,
    ToolResultEvent,
    CatalogQueryEvent,
    CatalogReplyEvent,
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
)
"""全部跨进程事件声明（各进程注册同一份集合即可互通）。"""

EVENT_NAMES: tuple[str, ...] = tuple(event.name for event in ALL_EVENTS)
"""全部事件名的只读清单（便于运维脚本与文档引用）。"""


def register_events(registry: EventRegistry) -> None:
    """把全部事件声明注册到事件注册表。"""
    for event in ALL_EVENTS:
        registry.register(event)
