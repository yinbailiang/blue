"""blue 用户界面：查看工具目录与会话。

```bash
uv run --package blue-service-inspect blue-service-inspect tools
uv run --package blue-service-inspect blue-service-inspect sessions
```
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import cast

from infinity_bus.templates.request import request

from blue.errors import BlueError
from blue.protocol import (
    CatalogQueryEvent,
    CatalogQueryResponse,
    CatalogReplyEvent,
    SessionStateEvent,
    SessionStateReplyEvent,
    SessionStateResponse,
)
from blue.runtime import build_runtime

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """目录 / 会话查看器入口。"""
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(
            _run(
                bus_url=cast(str | None, args.bus_url),
                what=cast(str, args.what),
                timeout=float(args.timeout),
            )
        )
    except BlueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


async def _run(*, bus_url: str | None, what: str, timeout: float) -> int:
    """按目标查询并打印。"""
    runtime = build_runtime("ui.inspect", url=bus_url)
    await runtime.start()
    try:
        proxy = runtime.proxy("ui.inspect")
        if what == "tools":
            catalog = await request(
                proxy,
                CatalogQueryEvent.name,
                {},
                CatalogReplyEvent.name,
                timeout=timeout,
            )
            if not isinstance(catalog, CatalogQueryResponse):
                raise BlueError("收到非预期的目录响应类型")
            print(f"工具目录 v{catalog.version}（{len(catalog.tools)} 个）")
            for tool in catalog.tools:
                print(f"  - {tool.function.name}: {tool.function.description}")
            return 0
        state = await request(
            proxy,
            SessionStateEvent.name,
            {},
            SessionStateReplyEvent.name,
            timeout=timeout,
        )
        if not isinstance(state, SessionStateResponse):
            raise BlueError("收到非预期的会话响应类型")
        print(f"共 {len(state.sessions)} 个会话")
        for info in state.sessions:
            print(f"  - {info.session_id}（{info.message_count} 条消息）")
        return 0
    finally:
        await runtime.stop()


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="blue-service-inspect", description="查看核心的工具目录与会话"
    )
    parser.add_argument("what", choices=["tools", "sessions"], help="要查看的内容")
    parser.add_argument(
        "--bus-url", default=None, help="总线地址（缺省读 BLUE_BUS_URL）"
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="等待秒数")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
