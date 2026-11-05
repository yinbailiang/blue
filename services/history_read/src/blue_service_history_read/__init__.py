"""blue 外置工具：按 id 读回会话历史的完整原文。

能力同样属于会话服务（``session.read``）；这里只做上下文传递与结果整理。
它和 ``history_search`` 配合：先搜到 id，再按 id 取回被压缩掉的原文。
"""

from __future__ import annotations

from typing import Annotated

from blue_contracts_session import (
    SessionCallError,
    SessionReadMessage,
)
from pydantic import BaseModel, Field

from blue_tool_sdk import current_context, run_tool, session_proxy

MAX_IDS = 20
"""一次最多读多少条。"""

MAX_CHARS_CEILING = 200_000
"""单条返回的字符上限。"""


class ReadResult(BaseModel):
    """读取结果。"""

    messages: list[SessionReadMessage] = Field(
        default_factory=list[SessionReadMessage],
        description="读到的消息（超长会截断并标注）",
    )
    missing: list[int] = Field(default_factory=list[int], description="不存在的 id")


async def history_read(
    ids: Annotated[list[int], "要读取的消息 id 列表（1~20 个）"],
    max_chars: Annotated[int, "每条返回的字符上限，默认 4000"] = 4000,
) -> ReadResult:
    """按 id 读回历史消息的完整原文（id 来自 history_search 的结果）。"""
    context = current_context()
    if not context.session_id:
        raise RuntimeError("history_read 只能在会话内调用（缺少 session_id）")
    if not ids:
        raise RuntimeError("ids 需要至少一个消息 id")
    try:
        response = await session_proxy().read(
            context.session_id,
            ids[:MAX_IDS],
            max_chars=max(200, min(max_chars, MAX_CHARS_CEILING)),
        )
    except SessionCallError as error:
        raise RuntimeError(str(error) or "读取失败") from error
    return ReadResult(messages=response.messages, missing=response.missing)


TOOL = history_read
"""单进程单工具：这个进程只提供 history_read。"""


def main() -> int:
    """控制台脚本入口（额外注册会话契约，才能反向调用 session 服务）。"""
    return run_tool(TOOL)
