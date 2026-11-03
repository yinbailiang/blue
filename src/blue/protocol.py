"""总线协议：事件负载模型与按命名空间生成的事件声明。

所有跨进程通信都走这里定义的负载模型。请求/响应类负载分别继承
:class:`RequestProtocol` / :class:`ResponseProtocol`，从而可直接复用
``infinity_bus.templates.request.request`` 的 RPC 语义（自动注入
``session_id`` / ``request_id`` 并按二者匹配响应）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from infinity_agent import ToolDefinition
from infinity_bus import EventDeclaration, EventRegistry
from infinity_bus.templates.request import RequestProtocol, ResponseProtocol
from pydantic import BaseModel, Field

__all__ = [
    "CatalogChangedPayload",
    "CatalogQueryRequest",
    "CatalogQueryResponse",
    "SessionInfo",
    "SessionPromptRequest",
    "SessionReplyResponse",
    "SessionResetRequest",
    "SessionResetResponse",
    "SessionStateRequest",
    "SessionStateResponse",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolHeartbeatPayload",
    "ToolRegisterPayload",
    "ToolUnregisterPayload",
    "Topic",
    "TopicBook",
]


class Topic(StrEnum):
    """总线主题（不含命名空间前缀）。"""

    TOOL_REGISTER = "tool.register"
    TOOL_HEARTBEAT = "tool.heartbeat"
    TOOL_UNREGISTER = "tool.unregister"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    CATALOG_QUERY = "catalog.query"
    CATALOG_REPLY = "catalog.reply"
    CATALOG_CHANGED = "catalog.changed"
    SESSION_PROMPT = "session.prompt"
    SESSION_REPLY = "session.reply"
    SESSION_RESET = "session.reset"
    SESSION_RESET_REPLY = "session.reset.reply"
    SESSION_STATE = "session.state"
    SESSION_STATE_REPLY = "session.state.reply"


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


# ---------------------------------------------------------------------------
# 工具调用（RPC）
# ---------------------------------------------------------------------------


class ToolCallRequest(RequestProtocol):
    """Agent 发起的工具调用请求。"""

    tool: str = Field(description="工具名")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")


class ToolCallResult(ResponseProtocol):
    """工具进程返回的调用结果。"""

    content: str = Field(default="", description="工具输出（字符串或 JSON 文本）")


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


class SessionResetRequest(RequestProtocol):
    """清空某个会话的历史。"""


class SessionResetResponse(ResponseProtocol):
    """会话重置结果。"""


class SessionInfo(BaseModel):
    """会话元信息（不含消息正文）。"""

    session_id: str = Field(description="会话 ID")
    message_count: int = Field(description="消息条数")
    updated_at: float = Field(description="最后更新时间（epoch 秒）")


class SessionStateRequest(RequestProtocol):
    """查询全部会话元信息。"""


class SessionStateResponse(ResponseProtocol):
    """会话元信息列表。"""

    sessions: list[SessionInfo] = Field(default_factory=list[SessionInfo])


_PAYLOADS: dict[Topic, type[BaseModel]] = {
    Topic.TOOL_REGISTER: ToolRegisterPayload,
    Topic.TOOL_HEARTBEAT: ToolHeartbeatPayload,
    Topic.TOOL_UNREGISTER: ToolUnregisterPayload,
    Topic.TOOL_CALL: ToolCallRequest,
    Topic.TOOL_RESULT: ToolCallResult,
    Topic.CATALOG_QUERY: CatalogQueryRequest,
    Topic.CATALOG_REPLY: CatalogQueryResponse,
    Topic.CATALOG_CHANGED: CatalogChangedPayload,
    Topic.SESSION_PROMPT: SessionPromptRequest,
    Topic.SESSION_REPLY: SessionReplyResponse,
    Topic.SESSION_RESET: SessionResetRequest,
    Topic.SESSION_RESET_REPLY: SessionResetResponse,
    Topic.SESSION_STATE: SessionStateRequest,
    Topic.SESSION_STATE_REPLY: SessionStateResponse,
}


class TopicBook:
    """按命名空间生成事件声明。

    同一命名空间下，各进程生成的事件名完全一致（``<namespace>.<topic>``），
    因此可跨进程互通；每个进程持有自己的 :class:`TopicBook` 与
    :class:`EventRegistry`。
    """

    def __init__(self, namespace: str = "blue") -> None:
        if not namespace.strip():
            raise ValueError("命名空间不能为空")
        self._namespace = namespace
        self._names: dict[Topic, str] = {}
        self._declarations: dict[Topic, type[EventDeclaration]] = {}
        for topic, payload in _PAYLOADS.items():
            name = f"{namespace}.{topic.value}"
            self._names[topic] = name
            self._declarations[topic] = _declare(topic, name, payload)

    @property
    def namespace(self) -> str:
        """当前命名空间。"""
        return self._namespace

    def name(self, topic: Topic) -> str:
        """主题的完整事件名（含命名空间）。"""
        return self._names[topic]

    def declaration(self, topic: Topic) -> type[EventDeclaration]:
        """主题对应的事件声明类。"""
        return self._declarations[topic]

    def topics(self) -> dict[Topic, str]:
        """全部主题名映射的副本。"""
        return dict(self._names)

    def register(self, registry: EventRegistry) -> None:
        """把全部事件声明注册到事件注册表（重复注册会抛错）。"""
        for declaration in self._declarations.values():
            registry.register(declaration)


def _declare(
    topic: Topic, name: str, payload: type[BaseModel]
) -> type[EventDeclaration]:
    """动态创建事件声明类。

    ``EventDeclaration`` 只依赖类属性 ``name`` / ``payload_type``，因此可按键
    命名空间动态生成，让事件名随配置变化。
    """
    class_name = f"{topic.name.title().replace('_', '')}Event"
    return cast(
        type[EventDeclaration],
        type(
            class_name,
            (EventDeclaration,),
            {"name": name, "payload_type": payload},
        ),
    )
