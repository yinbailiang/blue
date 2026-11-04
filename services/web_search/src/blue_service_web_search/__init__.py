"""blue 外置工具：网页搜索。

数据来自 Bing 的结果页（``https://cn.bing.com/search?q=...``，免费、无需密钥）。
只解析 ``li.b_algo`` 里的标题／链接／摘要，不做正文抓取——需要正文时让 agent
再用 ``browser_operate`` 或 ``read_file`` 打开具体页面，职责更清晰。

设计取舍：

- **多后端留了位置但只实现了一个**：``backend`` 字段进模型，方便以后接 Brave /
  SearXNG 而不改工具签名；现在只有 ``bing``；
- **UA 显式伪装成浏览器**：默认 UA 会被塞进「精简版」结果页，结构不同；
- **去重**：Bing 同一页会出现重复链接（不同子域别名），按 URL 去重；
- **失败语义**：抓不到就抛 ``ValueError``（agent 能看到原因），拿到页面但零结果
  则返回空列表 + ``note``（这是「查得太偏」而不是「工具坏了」）。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from blue.runtime import load_runtime_config, run_tool
from blue_service_web_search.parser import RawHit, parse_bing, unwrap_redirect

logger = logging.getLogger(__name__)

MAX_RESULTS = 10
"""单次返回条数上限（也是模型可见的 schema 约束）。"""

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
"""默认 UA 会被 Bing 降级成结构不同的精简页，所以伪装成浏览器。"""


class WebSearchConfig(BaseModel):
    """web_search 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(
        default="https://www.bing.com",
        description="搜索站点；默认国际版，国内直连时可用 cn.bing.com",
    )
    timeout: float = Field(default=15.0, gt=0, description="请求超时（秒）")
    user_agent: str = Field(default=DEFAULT_USER_AGENT, description="请求 UA")
    market: str = Field(
        default="auto",
        description="mkt 参数：auto 表示按查询语种自动选 zh-CN / en-US",
    )
    proxy: str = Field(
        default="",
        description="HTTP(S) 代理，例如 http://127.0.0.1:7890；空表示直连",
    )
    max_results: int = Field(
        default=MAX_RESULTS, ge=1, le=MAX_RESULTS, description="单次返回条数上限"
    )


class SearchHit(BaseModel):
    """一条搜索结果。"""

    rank: int = Field(description="排名（从 1 开始）")
    title: str = Field(description="标题")
    url: str = Field(description="原始链接")
    domain: str = Field(description="链接的域名")
    snippet: str = Field(default="", description="摘要")


class SearchResponse(BaseModel):
    """一次搜索的结果。"""

    query: str = Field(description="用户给出的查询串")
    executed: str = Field(default="", description="真正发出去的查询串（可能被简化）")
    backend: str = Field(description="后端名称")
    market: str = Field(default="", description="本次实际使用的 mkt")
    results: list[SearchHit] = Field(
        default_factory=list[SearchHit], description="搜索结果"
    )
    elapsed_ms: int = Field(default=0, description="耗时（毫秒）")
    note: str = Field(default="", description="附加说明（无结果、被限流等）")


_config: WebSearchConfig = WebSearchConfig()
"""进程级配置；由 :func:`configure` 在启动时注入。"""


def configure(config: WebSearchConfig) -> None:
    """设置本进程后续搜索使用的配置。"""
    global _config
    _config = config


async def web_search(
    query: Annotated[str, "搜索关键词，支持 site: 之类的搜索语法"],
    max_results: Annotated[
        int, Field(ge=1, le=MAX_RESULTS), "返回条数（1-10），默认 5"
    ] = 5,
    site: Annotated[str, "只搜索这个域名，例如 github.com"] = "",
) -> SearchResponse:
    """在网页上搜索关键词，返回标题、链接与摘要。"""
    text = query.strip()
    if not text:
        raise ValueError("query 不能为空")
    if not 1 <= max_results <= MAX_RESULTS:
        raise ValueError(f"max_results 必须在 1 到 {MAX_RESULTS} 之间")
    domain = site.strip().strip("/")
    if domain and "site:" not in text:
        text = f"{text} site:{domain}"
    limit = min(max_results, _config.max_results)
    return await _search(text, limit=limit, market=_market_for(text))


async def _search(query: str, *, limit: int, market: str) -> SearchResponse:
    """抓取结果页并整理成稳定模型。

    Bing 对 4 个词以上的长查询（尤其混着 ``JSON-RPC``、``tools/call`` 这类
    技术 token）会判成词典/商品意图，结果整页跑偏。所以这里做一次**相关性
    自检**：top 结果里几乎不含查询关键词时，用前几个词重试一次，并把「已简化」
    写进 ``note``。最多多花一次请求，但能救回这类查询。
    """
    started = time.monotonic()
    keywords = query_keywords(query)
    results = _collect(parse_bing(await _fetch(query, market=market)), limit=limit)
    executed = query
    note = ""
    if results and not results_relevant(results, keywords):
        for candidate in _shorter_queries(query):
            alternative = _collect(
                parse_bing(await _fetch(candidate, market=market)), limit=limit
            )
            if alternative and results_relevant(alternative, keywords):
                results = alternative
                executed = candidate
                note = f"原查询的结果与关键词不符，已简化为「{candidate}」重试"
                break
    elapsed = int((time.monotonic() - started) * 1000)
    if not results:
        note = "上游没有返回结果：可能是查询词太偏，或结果页被限流/改版"
    return SearchResponse(
        query=query,
        executed=executed,
        backend="bing",
        market=market,
        results=results,
        elapsed_ms=elapsed,
        note=note,
    )


def _collect(hits: list[RawHit], *, limit: int) -> list[SearchHit]:
    """把解析结果去重、编号、截断成展示模型。"""
    results: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        url = unwrap_redirect(hit.url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            SearchHit(
                rank=len(results) + 1,
                title=hit.title,
                url=url,
                domain=urlparse(url).netloc,
                snippet=hit.snippet,
            )
        )
        if len(results) >= limit:
            break
    return results


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "why",
        "when",
        "tutorial",
        "guide",
        "教程",
        "怎么",
        "如何",
    }
)
"""查询里不参与相关性判断的常见词。"""


def query_keywords(query: str) -> list[str]:
    """从查询里抽关键词：拉丁词按长度过滤，中文按连续片段切。"""
    words: list[str] = []
    buffer: list[str] = []
    for char in query:
        if char.isascii() and (char.isalnum() or char in "+#-_.:/"):
            buffer.append(char)
        else:
            if buffer:
                words.append("".join(buffer))
                buffer = []
            if not char.isascii() and not char.isspace():
                words.append(char)
    if buffer:
        words.append("".join(buffer))
    cleaned: list[str] = []
    for word in words:
        token = word.strip(".:/").lower()
        if len(token) < 2 or token in _STOPWORDS:
            continue
        if token not in cleaned:
            cleaned.append(token)
    return cleaned


def results_relevant(results: list[SearchHit], keywords: list[str]) -> bool:
    """前三名的标题/摘要/链接里，是否覆盖了至少一半关键词。"""
    if not keywords:
        return True
    blob = " ".join(
        f"{hit.title} {hit.snippet} {hit.url}".lower() for hit in results[:3]
    )
    matched = sum(1 for word in keywords if word in blob)
    return matched * 2 >= len(keywords)


def _shorter_queries(query: str) -> list[str]:
    """长查询的简化候选：逐步砍到 3 个词、2 个词。"""
    words = query.split()
    candidates: list[str] = []
    for size in (3, 2):
        if len(words) > size:
            candidates.append(" ".join(words[:size]))
    return candidates


async def _fetch(query: str, *, market: str) -> str:
    """请求结果页 HTML。

    :raises ValueError: 网络错误或上游返回非 2xx
    """
    url = f"{_config.base_url.rstrip('/')}/search"
    headers = {
        "User-Agent": _config.user_agent,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    }
    params: dict[str, str] = {"q": query}
    if market:
        params["mkt"] = market
    proxy = _config.proxy.strip() or None
    try:
        async with httpx.AsyncClient(
            timeout=_config.timeout, follow_redirects=True, proxy=proxy
        ) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.text
    except httpx.HTTPStatusError as error:
        raise ValueError(f"搜索服务返回 HTTP {error.response.status_code}") from error
    except httpx.HTTPError as error:
        raise ValueError(f"访问搜索服务失败：{error}") from error


def _market_for(query: str) -> str:
    """决定本次请求的 ``mkt``。

    显式配置了市场就用配置；``auto`` 时按查询语种猜测：含中日韩字符走
    ``zh-CN``，否则 ``en-US``。实测国际版对英文查询质量明显更好，而中文查询
    需要中文市场才能拿到中文站点。
    """
    configured = _config.market.strip()
    if configured and configured.lower() != "auto":
        return configured
    return "zh-CN" if _contains_cjk(query) else "en-US"


def _contains_cjk(text: str) -> bool:
    """查询里是否含中日韩文字。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


__all__ = [
    "CONFIG_MODEL",
    "DEFAULT_USER_AGENT",
    "MAX_RESULTS",
    "SearchHit",
    "SearchResponse",
    "WebSearchConfig",
    "configure",
    "main",
    "query_keywords",
    "results_relevant",
    "web_search",
]

CONFIG_MODEL = WebSearchConfig
"""契约测试使用的运行时配置模型。"""

TOOL = web_search
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure(load_runtime_config(WebSearchConfig))
    return run_tool(TOOL, "web_search")


if __name__ == "__main__":
    raise SystemExit(main())
