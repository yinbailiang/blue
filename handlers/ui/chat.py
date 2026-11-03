"""用户界面：命令行聊天客户端（外置程序）。

```bash
uv run python -m handlers.ui.chat --text "统计这段文字：hello world"
uv run python -m handlers.ui.chat --session demo --interactive
```

只通过总线与核心通信：发布 ``session.prompt``，等待 ``session.reply``。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from typing import cast

from infinity_bus.templates.request import request

from blue.config import DEFAULT_CONFIG_PATH
from blue.errors import BlueError
from blue.protocol import SessionReplyResponse, Topic, TopicBook
from blue.transport import BusRuntime
from handlers.support import build_runtime, load_settings_for_external

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """聊天客户端入口。"""
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(
            _run(
                config_path=cast(str, args.config),
                text=cast(str | None, args.text),
                session_id=cast(str | None, args.session),
                interactive=bool(args.interactive),
                timeout=float(args.timeout),
            )
        )
    except BlueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


async def _run(
    *,
    config_path: str,
    text: str | None,
    session_id: str | None,
    interactive: bool,
    timeout: float,
) -> int:
    """建立总线成员并按模式收发消息。"""
    settings = load_settings_for_external(config_path)
    topics = TopicBook(settings.bus.namespace)
    runtime = build_runtime(settings, "ui.chat")
    session = session_id or uuid.uuid4().hex
    await runtime.start()
    try:
        if interactive:
            await _chat_loop(runtime, topics, session, timeout)
            return 0
        if text is None:
            raise BlueError("需要 --text 或 --interactive")
        print(await _ask(runtime, topics, session, text, timeout))
        return 0
    finally:
        await runtime.stop()


async def _chat_loop(
    runtime: BusRuntime, topics: TopicBook, session: str, timeout: float
) -> None:
    """交互式对话（空行退出）。"""
    print(f"会话 {session} 已建立，输入空行退出。")
    while True:
        message = await asyncio.to_thread(input, "you> ")
        if not message.strip():
            return
        print(f"agent> {await _ask(runtime, topics, session, message, timeout)}")


async def _ask(
    runtime: BusRuntime,
    topics: TopicBook,
    session: str,
    text: str,
    timeout: float,
) -> str:
    """发送一条消息并取回核心的答复。"""
    response = await request(
        runtime.proxy("ui.chat"),
        topics.name(Topic.SESSION_PROMPT),
        {"text": text},
        topics.name(Topic.SESSION_REPLY),
        session_id=session,
        timeout=timeout,
    )
    if isinstance(response, SessionReplyResponse):
        if not response.success:
            raise BlueError(response.error_msg or "核心处理失败")
        return response.text
    raise BlueError("收到非预期的会话响应类型")


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m handlers.ui.chat", description="与核心对话"
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径"
    )
    parser.add_argument("--text", default=None, help="一次性消息")
    parser.add_argument("--session", default=None, help="会话 ID（默认新建）")
    parser.add_argument("--interactive", action="store_true", help="交互模式")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待秒数")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
