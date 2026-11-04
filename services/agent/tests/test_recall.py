"""内建历史回取工具：搜索、读原文，以及被 agent 拦在本地执行（不走总线）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from blue_contracts_session import (
    SessionPromptEvent,
    SessionReplyEvent,
    SessionReplyResponse,
)
from blue_service_agent.agent import AgentCore
from blue_service_agent.config import AgentConfig
from blue_service_agent.recall import (
    HISTORY_TOOL_NAMES,
    HISTORY_TOOLS,
    HistoryEntry,
    HistoryIndex,
)
from infinity_agent import FunctionCall, Message, MessageRole, ToolCall
from infinity_bus.templates.request import request
from tests.support import (
    MemoryRuntime,
    MemorySessionGateway,
    ScriptedClient,
    make_runtime,
)

from blue.transport import MemoryFanout

BIG_TOOL_RESULT = json.dumps({"rows": ["x" * 300] * 5}, ensure_ascii=False)
"""一条大 JSON 工具结果：模拟会被压缩掉的那种内容。"""


def _index() -> HistoryIndex:
    """一小段历史：system + 用户提问 + 调工具 + 大 JSON 结果。"""
    return HistoryIndex(
        [
            HistoryEntry(id=1, message=Message.system("你是测试")),
            HistoryEntry(id=2, message=Message.user("看看 tree.py 有没有循环引用")),
            HistoryEntry(
                id=3,
                message=Message.assistant(
                    content="我先搜一下",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            function=FunctionCall(
                                name="run_shell", arguments={"command": "grep tree.py"}
                            ),
                        )
                    ],
                ),
            ),
            HistoryEntry(
                id=4, message=Message.tool(content=BIG_TOOL_RESULT, tool_call_id="c1")
            ),
        ]
    )


def test_search_hits_by_substring_and_reports_position() -> None:
    """按子串搜索：命中给出片段、id、角色、在链上的序号。"""
    result = _index().search("tree.py")
    assert result["total"] == 1
    hit = result["hits"][0]
    assert hit["id"] == 2
    assert hit["role"] == "user"
    assert hit["index"] == 1
    assert "tree.py" in hit["snippet"]


def test_search_skips_system_unless_asked() -> None:
    """默认不搜 system 提示；显式指定角色时才搜。"""
    index = _index()
    assert index.search("你是测试")["total"] == 0
    assert index.search("你是测试", roles={MessageRole.SYSTEM})["total"] == 1


def test_search_respects_limit_but_reports_total() -> None:
    """命中很多时只回 limit 条，但 total 给出真实数量。"""
    many = HistoryIndex(
        [HistoryEntry(id=i, message=Message.user(f"关键词 {i}")) for i in range(1, 11)]
    )
    result = many.search("关键词", limit=3)
    assert len(result["hits"]) == 3
    assert result["total"] == 10


def test_read_returns_full_text_and_flags_truncation() -> None:
    """读原文给全量文本；超上限时截断并标注真实长度。"""
    index = _index()
    full = index.read([4], max_chars=100_000)["messages"][0]
    assert full["chars"] > 1000
    assert len(full["content"]) == full["chars"]

    clipped = index.read([4], max_chars=300)["messages"][0]
    assert clipped["truncated"] is True
    assert f"[{full['chars']} chars total]" in clipped["content"]


def test_read_reports_missing_ids() -> None:
    """不存在的 id 单独列出，避免误以为拿到了全部。"""
    payload = _index().read([2, 999])
    assert [item["id"] for item in payload["messages"]] == [2]
    assert payload["missing"] == [999]


def test_call_accepts_dict_and_json_string() -> None:
    """``call`` 同时接受 SDK 给的 dict 与 JSON 字符串；坏 JSON 变成错误文本。"""
    index = _index()
    from_dict = json.loads(index.call("history_search", {"query": "tree.py"}))
    from_str = json.loads(index.call("history_search", '{"query": "tree.py"}'))
    assert from_dict["total"] == from_str["total"] == 1
    assert "error" in json.loads(index.call("history_search", "{不是 json"))
    assert "error" in json.loads(index.call("history_read", {"ids": []}))


def test_tool_declarations_are_well_formed() -> None:
    """两个内建工具：名字固定、schema 合法、都在本地拦截名单里。"""
    names = [tool.function.name for tool in HISTORY_TOOLS]
    assert names == ["history_search", "history_read"]
    assert set(names) == set(HISTORY_TOOL_NAMES)
    for tool in HISTORY_TOOLS:
        assert tool.function.description
        assert tool.function.parameters.type == "object"
        assert tool.function.parameters.properties


@dataclass
class Harness:
    """跑起来的 agent：内存会话网关 + 脚本化模型。"""

    core: AgentCore
    gateway: MemorySessionGateway
    client: ScriptedClient
    chat: MemoryRuntime

    async def ask(self, text: str) -> SessionReplyResponse:
        """发一条用户消息，拿到 agent 的最终回复。"""
        reply = await request(
            self.chat.proxy(),
            SessionPromptEvent.name,
            {"text": text, "attachments": []},
            SessionReplyEvent.name,
            session_id="s1",
            timeout=20.0,
        )
        assert isinstance(reply, SessionReplyResponse)
        return reply


@asynccontextmanager
async def _harness(script: list[Message]) -> AsyncGenerator[Harness]:
    """起一套 agent（历史工具不需要工具进程，所以不启注册中心）。"""
    hub = MemoryFanout()
    gateway = MemorySessionGateway()
    client = ScriptedClient(script)
    core = AgentCore(
        runtime=make_runtime(hub, "agent"),
        settings=AgentConfig(api_key="sk-test", max_steps=4, system_prompt="sys"),
        client=client,
        session_gateway=gateway,
    )
    chat = make_runtime(hub, "chat")
    await core.start()
    await chat.start()
    try:
        yield Harness(core=core, gateway=gateway, client=client, chat=chat)
    finally:
        await chat.stop()
        await core.stop()


async def _agent_answers_history_tool_locally() -> None:
    """模型调 history_search 时应由 agent 本地执行并落库。"""
    script = [
        Message.assistant(
            content="",
            tool_calls=[
                ToolCall(
                    id="h1",
                    function=FunctionCall(
                        name="history_search", arguments={"query": "循环引用"}
                    ),
                )
            ],
        ),
        Message.assistant(content="找到了"),
    ]
    async with _harness(script) as run:
        # 先落一段「很早的历史」：里面有关键词
        await run.gateway.append(
            run.chat.proxy(),
            "s1",
            [
                Message.user("很早以前问过 tree.py 的循环引用"),
                Message.assistant(
                    content="查一下",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            function=FunctionCall(
                                name="run_shell",
                                arguments={"command": "grep -n cyclic tree.py"},
                            ),
                        )
                    ],
                ),
                Message.tool(content=BIG_TOOL_RESULT, tool_call_id="c1"),
            ],
        )
        reply = await run.ask("刚才那个循环引用是啥？")

    assert reply.success is True
    tools = [m for m in run.gateway.messages["s1"] if m.role == MessageRole.TOOL]
    assert tools, "history_search 的结果应当作为 tool 消息落库"
    payload = json.loads(str(tools[-1].content))
    assert payload["total"] == 1
    assert "tree.py" in payload["hits"][0]["snippet"]


async def _history_tools_do_not_touch_the_bus() -> None:
    """内建工具在本地跑：即使没有任何工具进程，也不会等到 tool_timeout。"""
    script = [
        Message.assistant(
            content="",
            tool_calls=[
                ToolCall(
                    id="h2",
                    function=FunctionCall(
                        name="history_read", arguments={"ids": [999]}
                    ),
                )
            ],
        ),
        Message.assistant(content="完了"),
    ]
    async with _harness(script) as run:
        reply = await run.ask("读一条不存在的消息")

    assert reply.success is True
    tools = [m for m in run.gateway.messages["s1"] if m.role == MessageRole.TOOL]
    payload = json.loads(str(tools[-1].content))
    assert payload["missing"] == [999]


def test_agent_answers_history_tool_locally() -> None:
    """模型调 ``history_search`` 时由 agent 本地执行：结果作为 tool 消息落库。"""
    asyncio.run(_agent_answers_history_tool_locally())


def test_history_tools_do_not_touch_the_bus() -> None:
    """内建工具在本地跑：即使没有任何工具进程，也不会等到 tool_timeout。"""
    asyncio.run(_history_tools_do_not_touch_the_bus())
