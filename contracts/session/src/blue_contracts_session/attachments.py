"""附件引用：内容寻址的伪 URL、哈希与 base64 归一化。

附件不直接以 base64 存在消息 JSON 里，而是存成本模块定义的伪 URL
（``blue+attachment://<sha256>``），真实字节放进会话库的 ``attachments`` 表：

- 同一张图在多个会话/多条消息里只存一份（主键就是内容哈希）；
- 消息 JSON 保持精简，模型侧由 agent 在发请求前 materialize 成 ``data:`` URL；
- WebUI 侧把它换成 ``/api/attachments/<hash>``，内容寻址天然可长缓存。

本模块只依赖标准库，契约包因此保持纯净。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, cast

__all__ = [
    "ATTACHMENT_URL_PREFIX",
    "attachment_url",
    "collect_attachment_hashes",
    "normalize_base64",
    "parse_attachment_url",
    "sha256_hex",
]

ATTACHMENT_URL_PREFIX = "blue+attachment://"
"""伪 URL 前缀；它不是真实协议，只在本系统内部流转。"""


def attachment_url(digest: str) -> str:
    """把内容哈希包装成伪 URL。"""
    return f"{ATTACHMENT_URL_PREFIX}{digest}"


def parse_attachment_url(url: str) -> str | None:
    """从伪 URL 中取出内容哈希；不是附件引用时返回 ``None``。"""
    if not url.startswith(ATTACHMENT_URL_PREFIX):
        return None
    digest = url[len(ATTACHMENT_URL_PREFIX) :].strip()
    return digest or None


def sha256_hex(data: bytes) -> str:
    """计算内容哈希（附件主键）。"""
    return hashlib.sha256(data).hexdigest()


def normalize_base64(data: str) -> tuple[str, str | None]:
    """归一化 base64 负载。

    :param data: 裸 base64，或 ``data:<mime>;base64,<payload>`` 形式的 data URL
    :return: （裸 base64, data URL 里声明的 mime 或 ``None``）

    :raises ValueError: 内容为空或不是合法 base64
    """
    payload = data.strip()
    declared: str | None = None
    if payload.startswith("data:"):
        header, separator, body = payload.partition(",")
        if not separator:
            raise ValueError("data URL 缺少逗号分隔的负载")
        meta = header[len("data:") :]
        declared = meta.split(";", 1)[0].strip() or None
        payload = body.strip()
    if not payload:
        raise ValueError("附件内容为空")
    try:
        base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError(f"附件不是合法的 base64：{error}") from error
    return payload, declared


def collect_attachment_hashes(payload: str) -> list[str]:
    """从消息 JSON 文本里按出现顺序收集附件哈希（去重）。

    :param payload: ``blue.messages`` 编码后的消息 JSON
    :return: 去重后的哈希列表；解析失败时返回空列表
    """
    try:
        decoded: object = json.loads(payload)
    except json.JSONDecodeError:
        return []
    found: list[str] = []
    seen: set[str] = set()
    _walk(decoded, found, seen)
    return found


def _walk(node: object, found: list[str], seen: set[str]) -> None:
    """深度优先遍历 JSON 结构，收集伪 URL 里的哈希。"""
    if isinstance(node, str):
        digest = parse_attachment_url(node)
        if digest is not None and digest not in seen:
            seen.add(digest)
            found.append(digest)
        return
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        for value in mapping.values():
            _walk(value, found, seen)
        return
    if isinstance(node, list):
        sequence = cast(
            list[str | float | int | bool | dict[str, Any] | list[Any] | None], node
        )
        for item in sequence:
            _walk(item, found, seen)
