"""MCP server 探测与预检测试：配置解析、命令检查、真实探测工具列表。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from blue.control import mcp

ROOT = Path(__file__).resolve().parents[1]
VSERVER = ROOT / "mcp_servers" / "virtual_screen" / "server.py"
"""仓库自带的 MCP server，探测它的工具列表不需要网络。"""


def test_declared_servers_parses_config() -> None:
    """server 声明被解析成结构化对象，非法条目跳过。"""
    config: dict[str, Any] = {
        "servers": [
            {
                "name": "kwin",
                "command": "mcp_servers/kwin_mcp/run.sh",
                "args": ["--screenshot-images"],
                "env": {"FOO": "bar"},
            },
            {"name": "off", "command": "x", "enabled": False},
            {"name": "no-command"},
            {"command": "no-name"},
            "not-a-dict",
        ]
    }

    servers = mcp.declared_servers(config)

    assert [server.name for server in servers] == ["kwin", "off"]
    assert servers[0].args == ("--screenshot-images",)
    assert servers[0].env == {"FOO": "bar"}
    assert servers[0].enabled is True
    assert servers[1].enabled is False
    assert servers[0].display.startswith("mcp_servers/kwin_mcp/run.sh")


def test_declared_servers_tolerates_missing_section() -> None:
    """没有 servers 段时返回空列表。"""
    assert mcp.declared_servers({}) == []
    assert mcp.declared_servers({"servers": "nope"}) == []


def test_declared_tool_names_and_unknown_servers() -> None:
    """白名单解析与「指向未声明 server」的检查。"""
    config = {"tools": ["kwin__screenshot", "ghost__thing", 42]}

    names = mcp.declared_tool_names(config)

    assert names == {"kwin__screenshot", "ghost__thing"}
    assert mcp.unknown_tool_servers(names, {"kwin"}) == ["ghost__thing"]
    assert mcp.unknown_tool_servers(names, {"kwin", "ghost"}) == []


def test_command_problem_reports_missing_path(tmp_path: Path) -> None:
    """相对命令按 cwd 解析，不存在时给出可读错误。"""
    problem = mcp.command_problem(".venv/bin/python", cwd=tmp_path)

    assert problem is not None
    assert "找不到可执行文件" in problem
    assert str(tmp_path) in problem


def test_command_problem_accepts_existing_executable(tmp_path: Path) -> None:
    """存在的可执行文件通过。"""
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    assert mcp.command_problem("./run.sh", cwd=tmp_path) is None
    # 不带斜杠的按 PATH 查找，与 subprocess 语义一致
    assert mcp.command_problem("run.sh", cwd=tmp_path) is not None
    assert mcp.command_problem(str(script), cwd=tmp_path) is None


def test_command_problem_rejects_non_executable(tmp_path: Path) -> None:
    """没有执行位也算问题。"""
    script = tmp_path / "data.txt"
    script.write_text("x", encoding="utf-8")
    script.chmod(0o644)

    problem = mcp.command_problem("./data.txt", cwd=tmp_path)

    assert problem is not None
    assert "没有执行权限" in problem


def test_command_problem_checks_path_lookup() -> None:
    """不带斜杠的命令走 PATH 查找。"""
    assert mcp.command_problem("python3", cwd=ROOT) is None
    assert mcp.command_problem("definitely-not-a-command-xyz", cwd=ROOT) is not None
    assert mcp.command_problem("   ", cwd=ROOT) is not None


@pytest.mark.skipif(shutil.which(sys.executable) is None, reason="需要可执行的 python")
def test_probe_tools_reads_real_server() -> None:
    """真实拉起仓库自带的 MCP server，拿回工具列表。"""
    server = mcp.DeclaredServer(
        name="vscreen",
        command=sys.executable,
        args=(str(VSERVER),),
    )

    tools = asyncio.run(mcp.probe_tools(server, cwd=ROOT, timeout=30.0))

    names = [tool.name for tool in tools]
    assert "screen_fill" in names
    assert "screen_snapshot" in names
    assert len(names) == 6


@pytest.mark.skipif(
    not (ROOT / ".venv" / "bin" / "python").exists(),
    reason="需要项目 venv（blue mcp-tools 会用它启动 vscreen）",
)
def test_cli_mcp_tools_reports_declared_coverage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 的 --json 输出里带 declared 标记。"""
    from blue import cli

    code = cli.main(
        [
            "--config",
            str(ROOT / "blue.infd"),
            "mcp-tools",
            "vscreen",
            "--json",
        ]
    )

    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["vscreen"]["error"] is None
    tools = payload["vscreen"]["tools"]
    assert [tool["name"] for tool in tools][:1] == ["screen_info"]
    assert all(tool["declared"] for tool in tools)
