"""session 服务的冒烟检查：真的写一条消息再读回来。

由 `blue apply` 在激活后调用（`service.infd` 里的 `smoke_command`），环境里带
本世代的 `BLUE_CONFIG_FILE`。**走总线而不是直连数据库**，于是一次检查同时覆盖
「总线通 + session 服务在 + 存储可写可读」三件事——这正是健康闸门（只看进程
就绪）看不到的部分。

检查用的是固定探针会话 `blue-smoke`，写完就 `reset` 清空，不碰任何真实对话。
"""

from __future__ import annotations

import asyncio
import os
import sys

from blue_contracts_session import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionResetEvent,
    SessionResetReplyEvent,
)
from infinity_agent import Message, MessageRole
from infinity_bus.templates.request import request

from blue.messages import decode_message, encode_message
from blue.runtime import build_runtime

__all__ = ["main"]

PROBE_SESSION = "blue-smoke"
"""探针会话 id：固定、与真实对话隔离。"""

MARKER = "blue-smoke-probe"
"""写进去的内容，读回来要能对上。"""

ROLE = "session.smoke"
"""总线成员名：必须与真正的 `session` 服务区分开，否则会共用 durable 队列。"""

_TIMEOUT = 10.0


async def _check() -> int:
    """返回 0 表示通过；非 0 时往 stderr 写原因。"""
    # 成员名决定 fanout 队列名，与真服务撞名会互相偷消息
    os.environ["BLUE_SERVICE_NAME"] = ROLE
    runtime = build_runtime(
        ROLE,
        events=[
            SessionGetEvent,
            SessionGetReplyEvent,
            SessionAppendEvent,
            SessionAppendReplyEvent,
            SessionResetEvent,
            SessionResetReplyEvent,
        ],
    )
    await runtime.start()
    try:
        proxy = runtime.proxy(ROLE)
        before = await request(
            proxy,
            SessionGetEvent.name,
            {"system_prompt": ""},
            SessionGetReplyEvent.name,
            session_id=PROBE_SESSION,
            timeout=_TIMEOUT,
        )
        if not isinstance(before, SessionGetResponse) or before.session is None:
            print("冒烟失败：session.get 没返回会话快照", file=sys.stderr)
            return 1
        baseline = len(before.session.nodes)

        message = Message(role=MessageRole.USER, content=MARKER)
        appended = await request(
            proxy,
            SessionAppendEvent.name,
            {"messages": [encode_message(message)]},
            SessionAppendReplyEvent.name,
            session_id=PROBE_SESSION,
            timeout=_TIMEOUT,
        )
        if not isinstance(appended, SessionAppendResponse):
            print("冒烟失败：session.append 没有正常应答", file=sys.stderr)
            return 1

        after = await request(
            proxy,
            SessionGetEvent.name,
            {"system_prompt": ""},
            SessionGetReplyEvent.name,
            session_id=PROBE_SESSION,
            timeout=_TIMEOUT,
        )
        if not isinstance(after, SessionGetResponse) or after.session is None:
            print("冒烟失败：写入后再读仍没有快照", file=sys.stderr)
            return 1
        if len(after.session.nodes) != baseline + 1:
            print(
                f"冒烟失败：写入后节点数 {len(after.session.nodes)}"
                f"（期望 {baseline + 1}）",
                file=sys.stderr,
            )
            return 1
        last = decode_message(after.session.nodes[-1].message)
        if MARKER not in str(last.content):
            print("冒烟失败：读回的消息内容对不上", file=sys.stderr)
            return 1

        await request(
            proxy,
            SessionResetEvent.name,
            {},
            SessionResetReplyEvent.name,
            session_id=PROBE_SESSION,
            timeout=_TIMEOUT,
        )
        return 0
    finally:
        await runtime.stop()


def main() -> int:
    """控制台入口。"""
    try:
        return asyncio.run(_check())
    except Exception as error:  # 冒烟失败要打印原因，而不是抛栈糊住 apply 输出
        print(f"冒烟失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
