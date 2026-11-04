"""Bing 搜索结果页解析：把 HTML 里的 ``li.b_algo`` 降维成结构化条目。

不引入 bs4 / lxml：结果页结构稳定，``html.parser`` 足够，也免得多一个依赖。
解析器刻意做成「容错优先」——上游改版时宁可少几条，也不要整体抛异常。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

__all__ = [
    "BingResultsParser",
    "RawHit",
    "parse_bing",
    "unwrap_redirect",
]

_BING_REDIRECT_HOSTS = {"www.bing.com", "cn.bing.com", "bing.com"}
"""这些域名下的 ``/ck/a`` 链接是点击统计跳转，需要还原真实 URL。"""

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class RawHit:
    """解析出的一条结果。"""

    title: str
    url: str
    snippet: str


class BingResultsParser(HTMLParser):
    """抽取 ``li.b_algo`` 里的标题、链接与摘要。

    结构大致是::

        <li class="b_algo">
          <h2><a href="https://...">标题</a></h2>
          <div class="b_caption"><p>摘要…</p></div>
        </li>
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[RawHit] = []
        self._active = False
        """是否位于一条结果内部。"""
        self._depth = 0
        """进入结果后 ``li`` 的嵌套深度。"""
        self._in_title = False
        self._in_snippet = False
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if (
            tag == "li"
            and not self._active
            and "b_algo" in attributes.get("class", "").split()
        ):
            self._active = True
            self._depth = 1
            self._in_title = False
            self._in_snippet = False
            self._title = []
            self._snippet = []
            self._url = ""
            return
        if not self._active:
            return
        if tag == "li":
            self._depth += 1
        elif tag == "h2" and not self._url:
            self._in_title = True
        elif tag == "a" and self._in_title and not self._url:
            self._url = attributes.get("href", "")
        elif tag == "p":
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if tag == "h2":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            self._depth -= 1
            if self._depth == 0:
                self._finish()
                self._active = False

    def handle_data(self, data: str) -> None:
        if not self._active:
            return
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def _finish(self) -> None:
        """收尾一条结果：折叠空白、还原跳转链接。"""
        title = " ".join("".join(self._title).split())
        snippet = " ".join("".join(self._snippet).split())
        url = unwrap_redirect(self._url)
        if url and title:
            self.results.append(RawHit(title=title, url=url, snippet=snippet))


def parse_bing(html: str) -> list[RawHit]:
    """解析 Bing 结果页；解析失败时返回已抽到的部分。"""
    parser = BingResultsParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return parser.results
    return parser.results


def unwrap_redirect(url: str) -> str:
    """还原 Bing 的点击统计跳转（``/ck/a?...&u=a1<base64url>``）。

    非跳转链接原样返回；解码失败也原样返回，避免把可用链接弄丢。
    """
    text = url.strip()
    if not text.startswith(("http://", "https://", "/")):
        return text
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if host and host not in _BING_REDIRECT_HOSTS:
        return text
    if not parsed.path.startswith("/ck/"):
        return text
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded:
        return text
    payload = encoded[2:] if encoded.startswith("a1") else encoded
    if _BASE64URL.fullmatch(payload) is None:
        return text
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding).decode(
            "utf-8", errors="replace"
        )
    except ValueError, TypeError:
        return text
    decoded = unquote(decoded).strip()
    if decoded.startswith(("http://", "https://")):
        return decoded
    return text
