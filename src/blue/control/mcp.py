"""MCP server 的声明信息、启动预检与工具探测。

分两层，故意不耦合：

- **静态层**（本模块主体）：从 ``blue.infd`` 的 ``mcp_bridge.servers`` 配置里读出
  server 声明、检查启动命令能不能执行。只依赖标准库，``blue doctor`` 用它做
  部署前预检，没装 MCP 桥也能用。
- **探测层**（:func:`probe_tools`）：真的把 server 拉起来握手、拿 ``tools/list``。
  它延迟导入 ``blue_service_mcp_bridge``，因此核心包不硬依赖任何服务包。

后者的用途很具体：``mcp_bridge.tools`` 白名单要求写对工具名，而不同版本的
server 工具名会变（kwin-mcp 0.12 叫 ``list_windows``，1.0 改名 ``window_list``），
靠翻 README 猜很容易踩坑——直接把 server 的工具名打出来照着抄即可。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

__all__ = [
    "DeclaredServer",
    "ToolSummary",
    "command_problem",
    "declared_servers",
    "declared_tool_names",
    "probe_tools",
    "unknown_tool_servers",
]


@dataclass(frozen=True)
class DeclaredServer:
    """``mcp_bridge.servers`` 里的一条声明。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict[str, str])
    enabled: bool = True

    @property
    def display(self) -> str:
        """命令行的人类可读形式。"""
        return " ".join([self.command, *self.args])


@dataclass(frozen=True)
class ToolSummary:
    """server 提供的一个工具。"""

    name: str
    description: str = ""


def declared_servers(config: dict[str, Any]) -> list[DeclaredServer]:
    """从 mcp_bridge 服务配置里读出 server 声明（跳过非法条目）。"""
    raw = config.get("servers")
    if not isinstance(raw, list):
        return []
    servers: list[DeclaredServer] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        name = entry.get("name")
        command = entry.get("command")
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        servers.append(
            DeclaredServer(
                name=name,
                command=command,
                args=_str_tuple(entry.get("args")),
                env=_str_map(entry.get("env")),
                enabled=entry.get("enabled", True) is not False,
            )
        )
    return servers


def declared_tool_names(config: dict[str, Any]) -> set[str]:
    """白名单里的工具名（形如 ``<server>__<tool>``）。"""
    raw = config.get("tools")
    if not isinstance(raw, list):
        return set()
    return {item for item in cast(list[object], raw) if isinstance(item, str)}


def unknown_tool_servers(tool_names: set[str], server_names: set[str]) -> list[str]:
    """白名单里指向了未声明 server 的条目（配置层已校验，这里给 doctor 复核）。"""
    broken: list[str] = []
    for entry in sorted(tool_names):
        server = entry.split("__", 1)[0]
        if server not in server_names:
            broken.append(entry)
    return broken


def command_problem(command: str, *, cwd: Path) -> str | None:
    """检查启动命令能否执行；返回问题描述，没问题返回 ``None``。

    约定与 :class:`subprocess` 一致：带 ``/`` 的按路径解析（相对路径基于
    ``cwd``，也就是项目根），否则查 ``PATH``。
    """
    text = command.strip()
    if not text:
        return "启动命令为空"
    if "/" not in text:
        if shutil.which(text) is None:
            return f"PATH 里找不到命令：{text}"
        return None
    path = Path(text).expanduser()
    candidate = path if path.is_absolute() else (cwd / path)
    if not candidate.exists():
        return f"找不到可执行文件：{candidate}"
    if not candidate.is_file():
        return f"不是普通文件：{candidate}"
    if not os.access(candidate, os.X_OK):
        return f"没有执行权限：{candidate}"
    return None


async def probe_tools(
    server: DeclaredServer, *, cwd: Path, timeout: float = 30.0
) -> list[ToolSummary]:
    """启动 server、握手并取工具列表（用完即关）。

    :raises RuntimeError: MCP 桥的服务包未安装
    :raises Exception: 启动失败或握手超时（原样上抛，由调用方展示）
    """
    try:
        from blue_service_mcp_bridge.client import StdioConnector
        from blue_service_mcp_bridge.config import McpServerConfig
    except ImportError as error:  # pragma: no cover - 只在缺包时走到
        raise RuntimeError(
            "探测 MCP 工具需要 blue-service-mcp-bridge（uv sync --all-packages）"
        ) from error

    config = McpServerConfig(
        name=server.name,
        command=server.command,
        args=list(server.args),
        env=dict(server.env),
    )
    session = await StdioConnector(cwd=cwd, timeout=timeout).connect(config)
    try:
        tools = await session.list_tools()
    finally:
        await session.close()
    return [ToolSummary(name=tool.name, description=tool.description) for tool in tools]


def _str_tuple(value: object) -> tuple[str, ...]:
    """取字符串数组。"""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast(list[object], value) if isinstance(item, str))


def _str_map(value: object) -> dict[str, str]:
    """取字符串字典。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
        if isinstance(item, str)
    }
