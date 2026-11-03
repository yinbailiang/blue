"""会话历史自愈：清洗会让模型接口 400 的畸形消息序列。

会话按写前日志落库：assistant 消息（含 ``tool_calls``）先入库，工具结果再逐条
追加。进程若死在两者之间，历史里就会留下没有结果的 ``tool_call``，此后每次
请求都会被 OpenAI/DeepSeek 兼容接口以「insufficient tool messages following
tool_calls」拒收，且重启无效——坏数据在持久层。

本模块在把历史交给模型之前做纯内存修复，不改动持久化数据：

- 缺少结果的 ``tool_call`` 补写占位工具消息（保序插在已有结果之后）；
- 找不到归属的孤儿 ``tool`` 消息丢弃；
- 既无内容也无 ``tool_calls`` 的 assistant 消息丢弃。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from infinity_agent import Message, MessageRole, ToolCall

__all__ = ["sanitize_history"]


def _placeholder(call: ToolCall) -> str:
    """为缺失的工具结果生成占位文本（JSON 形态与 agent 的错误回灌一致）。"""
    return json.dumps(
        {
            "error": (
                f"工具 {call.function.name} 的结果缺失：调用期间进程中断，"
                "该次调用没有返回值，不要假设其结果。"
            )
        },
        ensure_ascii=False,
    )


def _has_content(message: Message) -> bool:
    """assistant 消息是否带有可发送的文本内容。"""
    return isinstance(message.content, str) and bool(message.content.strip())


def sanitize_history(messages: Iterable[Message]) -> list[Message]:
    """返回可安全发给模型的历史副本。

    :param messages: 从会话库还原出的历史消息，顺序即时间顺序。
    :return: 修复后的新列表；入参不受影响。
    """
    source = list(messages)
    cleaned: list[Message] = []
    index = 0
    while index < len(source):
        message = source[index]
        if message.role == MessageRole.ASSISTANT and message.tool_calls:
            calls = list(message.tool_calls)
            declared = {call.id for call in calls}
            index += 1
            answered: set[str] = set()
            cleaned.append(message)
            while index < len(source) and source[index].role == MessageRole.TOOL:
                tool_message = source[index]
                index += 1
                call_id = tool_message.tool_call_id
                if call_id in declared and call_id not in answered:
                    answered.add(call_id)
                    cleaned.append(tool_message)
            for call in calls:
                if call.id not in answered:
                    cleaned.append(
                        Message.tool(content=_placeholder(call), tool_call_id=call.id)
                    )
            continue
        index += 1
        if message.role == MessageRole.TOOL:
            continue
        if message.role == MessageRole.ASSISTANT and not _has_content(message):
            continue
        cleaned.append(message)
    return cleaned
