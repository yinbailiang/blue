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


EVENTS: tuple[type[EventDeclaration], ...] = (
    ToolRegisterEvent,
    ToolHeartbeatEvent,
    ToolUnregisterEvent,
    CatalogChangedEvent,
    ToolCallEvent,
    ToolResultEvent,
    CatalogQueryEvent,
    CatalogReplyEvent,
)
"""工具与目录领域全部跨进程事件声明。"""
