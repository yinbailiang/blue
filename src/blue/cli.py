"""``blue`` 核心命令行：本体聚合的唯一入口。

核心只管理自己——以聚合形式启动（会话 + 工具目录）或校验配置。工具进程、
用户界面等外围程序都是独立进程（见仓库根目录 ``handlers/``），由使用者自行
启动，本命令行不提供它们的启动或管理入口。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from typing import cast

from blue.aggregate import AgentAggregate
from blue.config import DEFAULT_CONFIG_PATH, BlueSettings, ConfigError, load_settings
from blue.errors import BlueError
from blue.lifecycle import serve_forever
from blue.transport import RabbitRuntime

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回进程退出码。"""
    args = _parser().parse_args(argv)
    try:
        match args.command:
            case "serve":
                asyncio.run(serve_forever(_build_aggregate(cast(str, args.config))))
                return 0
            case "config-check":
                return _config_check(cast(str, args.config))
            case _:
                print(f"未知命令：{args.command}", file=sys.stderr)
                return 2
    except BlueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _config_check(path: str) -> int:
    """编译并校验配置文件。"""
    settings = load_settings(path)
    print(f"配置有效：{path}")
    print(f"  agent   : {settings.agent.provider}/{settings.agent.model}")
    print(f"  传输    : {settings.bus.transport} @ {settings.bus.url}")
    print(f"  租约    : ttl={settings.registry.ttl}s")
    return 0


def _build_aggregate(config_path: str) -> AgentAggregate:
    """按配置装配本体聚合。"""
    settings = load_settings(config_path)
    return AgentAggregate(runtime=_build_runtime(settings), settings=settings)


def _build_runtime(settings: BlueSettings) -> RabbitRuntime:
    """创建聚合自身的总线成员（一个进程一个成员）。"""
    if settings.bus.transport != "rabbit":
        raise ConfigError("本体聚合需要 rabbit 传输；memory 传输仅用于进程内测试")
    return RabbitRuntime(
        url=settings.bus.url,
        member_id=f"core.{uuid.uuid4().hex[:8]}",
    )


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="blue", description="Agent 本体聚合")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="以本体聚合身份运行（会话中心 + 工具目录）")
    subparsers.add_parser("config-check", help="校验配置文件")
    return parser
