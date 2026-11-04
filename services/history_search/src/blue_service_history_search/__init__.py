"""blue 外置工具：在本会话的历史里搜索（压缩掉的旧对话也能查到）。

能力本身属于会话服务（``session.search``）——历史归它所有，用 SQL 在库里搜；
这里只是一层薄包装：拿工具调用上下文里的 ``session_id``，转成一次 RPC，
再把结果整理成给模型看的样子。

为什么不做成 agent 内建工具：内建的看不见（不在可用工具目录里）、改一次要重启
agent、也没法给 webui / MCP 桥复用。
"""

from __future__ import annotations

from typing import Annotated

from blue_contracts_session import (
    EVENTS as SESSION_EVENTS,
)
from blue_contracts_session import (
    SessionSearchEvent,
    SessionSearchHit,
    SessionSearchReplyEvent,
    SessionSearchResponse,
)
from pydantic import BaseModel, Field

from blue.runtime import run_tool
from blue.tool_context import call_service, current_context

MAX_LIMIT = 50
"""一次搜索最多返回多少条命中。"""


class SearchResult(BaseModel):
    """搜索结果。"""

    total: int = Field(description="命中总数（可能大于 hits 的条数）")
    hits: list[SessionSearchHit] = Field(
        default_factory=list[SessionSearchHit], description="命中列表"
    )


async def history_search(
    query: Annotated[str, "要搜索的文本（子串匹配，可为命令名、报错、关键词）"],
    limit: Annotated[int, f"最多返回多少条命中，1~{MAX_LIMIT}，默认 5"] = 5,
    roles: Annotated[
        str, "只看这些角色，逗号分隔；默认 user,assistant,tool（不含 system）"
    ] = "user,assistant,tool",
) -> SearchResult:
    """在本会话的历史里按子串搜索。

    上下文里出现「早期历史已省略」、或某条消息被省略/截断时，用它定位，
    再用 history_read 取回原文。
    """
    context = current_context()
    if not context.session_id:
        raise RuntimeError("history_search 只能在会话内调用（缺少 session_id）")
    response = await call_service(
        SessionSearchEvent.name,
        {"query": query, "limit": max(1, min(limit, MAX_LIMIT)), "roles": roles},
        SessionSearchReplyEvent.name,
        session_id=context.session_id,
    )
    if not isinstance(response, SessionSearchResponse):
        raise RuntimeError("收到非预期的搜索应答")
    if not response.success:
        raise RuntimeError(response.error_msg or "搜索失败")
    return SearchResult(total=response.total, hits=response.hits)


TOOL = history_search
"""单进程单工具：这个进程只提供 history_search。"""


def main() -> int:
    """控制台脚本入口（额外注册会话契约，才能反向调用 session 服务）。"""
    return run_tool(TOOL, "history_search", events=SESSION_EVENTS)
