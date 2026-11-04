"""分支名要能穿过总线 RPC：写进去 → 快照里必须读得到。

补的是一个真实遗漏：``set_label`` 落库成功、``session.get`` 却读不到 label
（``_snapshot`` 忘了带字段）。只有走 RPC 的用例才能发现，纯数据库测试发现不了。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue_contracts_session import (
    SessionAppendEvent,
    SessionAppendReplyEvent,
    SessionAppendResponse,
    SessionGetEvent,
    SessionGetReplyEvent,
    SessionGetResponse,
    SessionNodeLabelEvent,
    SessionNodeLabelReplyEvent,
    SessionNodeLabelResponse,
)
from blue_service_session.config import SessionConfig
from blue_service_session.service import SessionService
from infinity_agent import Message
from infinity_bus import EventBus
from infinity_bus.templates.request import request
from tests.support import make_runtime

from blue.messages import encode_message
from blue.transport import MemoryFanout


async def _stack(tmp_path: Path) -> tuple[SessionService, EventBus.Proxy]:
    """起 session 服务 + 调用方代理。"""
    hub = MemoryFanout()
    service = SessionService(
        config=SessionConfig(database_path=str(tmp_path / "sessions.db")),
        runtime=make_runtime(hub, "session"),
    )
    client = make_runtime(hub, "check")
    await service.start()
    await client.start()
    # 先把会话建出来（session.get 会 ensure）
    await request(
        client.proxy(),
        SessionGetEvent.name,
        {"system_prompt": "sys"},
        SessionGetReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    return service, client.proxy()


async def _append(
    proxy: EventBus.Proxy, message: Message, parent: int | None = None
) -> int:
    """追加一条消息，返回新节点 id。"""
    payload: dict[str, object] = {"messages": [encode_message(message)]}
    if parent is not None:
        payload["parent"] = parent
    response = await request(
        proxy,
        SessionAppendEvent.name,
        payload,
        SessionAppendReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    assert isinstance(response, SessionAppendResponse)
    return response.nodes[0]


async def _labels(proxy: EventBus.Proxy) -> dict[int, str]:
    """读一次快照，取出每个节点的分支名。"""
    response = await request(
        proxy,
        SessionGetEvent.name,
        {"system_prompt": "sys"},
        SessionGetReplyEvent.name,
        session_id="s1",
        timeout=10.0,
    )
    assert isinstance(response, SessionGetResponse)
    assert response.session is not None
    return {node.id: node.label for node in response.session.nodes}


def test_label_survives_the_rpc_roundtrip(tmp_path: Path) -> None:
    """改名后 session.get 的快照里能读到名字；清空后变回空串。"""
    asyncio.run(_label_roundtrip(tmp_path))


async def _label_roundtrip(tmp_path: Path) -> None:
    service, proxy = await _stack(tmp_path)
    try:
        question = await _append(proxy, Message.user("问"))
        first = await _append(proxy, Message.assistant(content="答 A"))
        second = await _append(
            proxy, Message.assistant(content="答 B"), parent=question
        )

        renamed = await request(
            proxy,
            SessionNodeLabelEvent.name,
            {"node": first, "label": "  宽松 版 "},
            SessionNodeLabelReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(renamed, SessionNodeLabelResponse)
        assert renamed.success is True
        assert renamed.label == "宽松 版"

        labels = await _labels(proxy)
        assert labels[first] == "宽松 版", "改名必须出现在快照里（否则前端看不到）"
        assert labels[second] == ""
        assert labels[question] == ""

        cleared = await request(
            proxy,
            SessionNodeLabelEvent.name,
            {"node": first, "label": ""},
            SessionNodeLabelReplyEvent.name,
            session_id="s1",
            timeout=10.0,
        )
        assert isinstance(cleared, SessionNodeLabelResponse)
        assert cleared.label == ""
        assert (await _labels(proxy))[first] == ""
    finally:
        await service.stop()
