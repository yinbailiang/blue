"""工具与目录领域的事件契约。

本模块只依赖 ``pydantic`` / ``infinity_bus`` / ``infinity_agent``，
不依赖 ``blue`` 或任何服务实现，避免契约层被运行时依赖污染。
"""

from __future__ import annotations

from typing import Any

from infinity_agent import ToolDefinition
from infinity_bus import EventDeclaration
from infinity_bus.templates.request import RequestProtocol, ResponseProtocol
from pydantic import BaseModel, Field

__all__ = [
    "EVENTS",
    "CatalogChangedEvent",
    "CatalogChangedPayload",
    "CatalogQueryEvent",
    "CatalogQueryRequest",
    "CatalogQueryResponse",
    "CatalogReplyEvent",
    "ToolAttachment",
    "ToolCallEvent",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolDefinition",
    "ToolHeartbeatEvent",
    "ToolHeartbeatPayload",
    "ToolRegisterEvent",
    "ToolRegisterPayload",
    "ToolResultEvent",
    "ToolUnregisterEvent",
    "ToolUnregisterPayload",
]


# ---------------------------------------------------------------------------
# 工具注册中心
# ---------------------------------------------------------------------------


class ToolRegisterPayload(RequestProtocol):
    """工具进程上线：把自己登记到注册中心。

    这是**请求/应答**（不是单向广播）：注册中心是单实例，进程需要知道登记结果
    （目录变没变、被拒了没有）。以后若要支持多实例，应设计专门的协议再迁移。
    """

    worker_id: str = Field(description="工具进程唯一标识")
    tool: ToolDefinition = Field(description="工具定义（由函数签名自动推断）")
    ttl: float = Field(description="租约时长（秒），超时未续约即被清除")


class ToolRegisterResponse(ResponseProtocol):
    """登记结果。"""

    changed: bool = Field(default=False, description="目录是否因这次登记而变化")
    version: int = Field(default=0, description="登记后的目录版本号")


class ToolHeartbeatPayload(RequestProtocol):
    """工具进程续约；``registered=False`` 表示租约已失效、调用方应重新登记。"""

    worker_id: str = Field(description="工具进程唯一标识")


class ToolHeartbeatResponse(ResponseProtocol):
    """续约结果。"""

    registered: bool = Field(default=False, description="该进程当前是否仍在册")


class ToolUnregisterPayload(RequestProtocol):
    """工具进程主动下线。"""

    worker_id: str = Field(description="工具进程唯一标识")


class ToolUnregisterResponse(ResponseProtocol):
    """下线结果。"""

    removed: bool = Field(default=False, description="是否真的移除了登记")
    changed: bool = Field(default=False, description="目录是否因这次下线而变化")


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


class ToolRegisterReplyEvent(EventDeclaration):
    """登记结果。"""

    name = "tool.register.reply"
    payload_type = ToolRegisterResponse


class ToolHeartbeatEvent(EventDeclaration):
    """工具进程续约。"""

    name = "tool.heartbeat"
    payload_type = ToolHeartbeatPayload


class ToolHeartbeatReplyEvent(EventDeclaration):
    """续约结果。"""

    name = "tool.heartbeat.reply"
    payload_type = ToolHeartbeatResponse


class ToolUnregisterEvent(EventDeclaration):
    """工具进程主动下线。"""

    name = "tool.unregister"
    payload_type = ToolUnregisterPayload


class ToolUnregisterReplyEvent(EventDeclaration):
    """下线结果。"""

    name = "tool.unregister.reply"
    payload_type = ToolUnregisterResponse


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


class ToolAttachment(BaseModel):
    """工具返回的附件（图片等）。

    工具把 ``attachments`` 字段放进返回对象，``ToolWorker`` 会把它从正文里摘出来
    放进 ``ToolCallResult.attachments``；agent 再把它转成内容寻址的会话附件。
    """

    mime: str = Field(description="MIME 类型，例如 image/png")
    data: str = Field(description="base64 负载（不含 data: 前缀）")
    name: str | None = Field(default=None, description="可选文件名")


class ToolCallResult(ResponseProtocol):
    """工具进程返回的调用结果。"""

    content: str = Field(default="", description="工具输出（字符串或 JSON 文本）")
    attachments: list[ToolAttachment] = Field(
        default_factory=list[ToolAttachment],
        description="结构化附件（图片等），由 agent 转成内容寻址存储",
    )


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


EVENTS: tuple[type[EventDeclaration], ...] = (
    ToolRegisterEvent,
    ToolRegisterReplyEvent,
    ToolHeartbeatEvent,
    ToolHeartbeatReplyEvent,
    ToolUnregisterEvent,
    ToolUnregisterReplyEvent,
    CatalogChangedEvent,
    ToolCallEvent,
    ToolResultEvent,
    CatalogQueryEvent,
    CatalogReplyEvent,
)
"""工具与目录领域全部跨进程事件声明。"""
