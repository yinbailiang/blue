"""web_search 工具测试：结果页解析、参数拼装与错误处理。

网络一律用 ``httpx.MockTransport`` 替掉，测试只依赖仓库里的真实结果页片段
（``fixtures/bing_results.html``，从 cn.bing.com 抓取后裁剪）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import blue_service_web_search as search
import httpx
import pytest
from blue_service_web_search import (
    WebSearchConfig,
    query_keywords,
    results_relevant,
    web_search,
)
from blue_service_web_search.parser import parse_bing, unwrap_redirect

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bing_results.html"
"""两条真实结果的 HTML 片段。"""

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程级配置不泄漏到其它用例。"""
    monkeypatch.setattr(search, "_config", WebSearchConfig())


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """把 httpx.AsyncClient 换成带 MockTransport 的版本。"""
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(search.httpx, "AsyncClient", factory)


def _html_handler(seen: dict[str, Any] | None = None) -> Handler:
    """返回固定 HTML 的 handler，并把请求细节记进 ``seen``。"""
    html = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["q"] = request.url.params.get("q")
            seen["mkt"] = request.url.params.get("mkt")
            seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text=html)

    return handler


def test_fixture_parses_two_results() -> None:
    """fixture 里两条结果都能抽出来（标题/链接/摘要）。"""
    hits = parse_bing(FIXTURE.read_text(encoding="utf-8"))

    assert len(hits) == 2
    assert hits[0].title.startswith("Python asyncio")
    assert hits[0].url.startswith("https://")
    assert hits[0].snippet
    assert hits[1].title
    assert hits[1].url != hits[0].url


def test_search_returns_structured_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：抓页 → 解析 → 结构化返回。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _html_handler(seen))

    result = asyncio.run(web_search("python asyncio", max_results=2))

    assert result.query == "python asyncio"
    assert result.backend == "bing"
    assert [hit.rank for hit in result.results] == [1, 2]
    assert result.results[0].title.startswith("Python asyncio")
    assert result.results[0].domain
    assert result.results[0].snippet
    assert result.note == ""
    assert result.elapsed_ms >= 0
    assert seen["q"] == "python asyncio"
    assert seen["mkt"] == "en-US"
    assert result.market == "en-US"
    assert "Mozilla" in str(seen["ua"])


def test_site_filter_is_appended_to_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """``site`` 参数以 site: 语法拼进查询串。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _html_handler(seen))

    asyncio.run(web_search("asyncio", site="github.com"))

    assert seen["q"] == "asyncio site:github.com"


def test_site_filter_is_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询串里已经有 site: 时不再重复拼接。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _html_handler(seen))

    asyncio.run(web_search("asyncio site:github.com", site="github.com"))

    assert seen["q"] == "asyncio site:github.com"


def test_max_results_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_results 决定返回条数，rank 连续。"""
    _patch_client(monkeypatch, _html_handler())

    result = asyncio.run(web_search("python", max_results=1))

    assert len(result.results) == 1
    assert result.results[0].rank == 1


def test_empty_page_reports_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """抓到页面但没有结果时，返回空列表 + 说明，而不是报错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="<html><body>no results</body></html>")

    _patch_client(monkeypatch, handler)

    result = asyncio.run(web_search("qqqzzz"))

    assert result.results == []
    assert "没有返回结果" in result.note


def test_http_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游 5xx 转成可读错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text="busy")

    _patch_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="HTTP 503"):
        asyncio.run(web_search("python"))


def test_connection_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络异常转成可读错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="访问搜索服务失败"):
        asyncio.run(web_search("python"))


def test_rejects_empty_query() -> None:
    """空查询直接拒绝。"""
    with pytest.raises(ValueError, match="query 不能为空"):
        asyncio.run(web_search("   "))


def test_rejects_out_of_range_max_results() -> None:
    """条数越界直接拒绝。"""
    with pytest.raises(ValueError, match="max_results"):
        asyncio.run(web_search("python", max_results=99))


def test_unwrap_redirect_decodes_bing_click_tracking() -> None:
    """Bing 的 /ck/a 跳转链接会被还原成真实 URL。"""
    encoded = "a1aHR0cHM6Ly9leGFtcGxlLmNvbS9wYXRoP2E9MQ"
    wrapped = f"https://www.bing.com/ck/a?!&&p=abc&u={encoded}&ntb=1"

    assert unwrap_redirect(wrapped) == "https://example.com/path?a=1"


def test_unwrap_redirect_keeps_plain_urls() -> None:
    """普通链接原样返回。"""
    assert unwrap_redirect("https://example.com/x") == "https://example.com/x"
    assert unwrap_redirect("") == ""


def test_chinese_query_uses_chinese_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """含中文的查询自动切到 zh-CN。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _html_handler(seen))

    result = asyncio.run(web_search("北京 天气"))

    assert seen["mkt"] == "zh-CN"
    assert result.market == "zh-CN"


def test_configure_applies_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure() 生效：base_url / market / proxy 会体现在配置上。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _html_handler(seen))
    search.configure(
        WebSearchConfig(
            base_url="https://example.test",
            market="en-GB",
            proxy="http://127.0.0.1:7890",
        )
    )

    asyncio.run(web_search("python"))

    assert seen["url"].startswith("https://example.test/search")
    assert seen["mkt"] == "en-GB"


def test_auto_market_is_default() -> None:
    """默认配置：国际版 + 自动市场 + 无代理。"""
    config = WebSearchConfig()

    assert config.base_url == "https://www.bing.com"
    assert config.market == "auto"
    assert config.proxy == ""


def test_config_model_is_exported() -> None:
    """契约测试依赖 CONFIG_MODEL 与 TOOL 两个符号。"""
    assert search.CONFIG_MODEL is WebSearchConfig
    assert search.TOOL is web_search
    assert search.MAX_RESULTS == 10


OFFTOPIC_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://baike.baidu.com/item/Model">Model（英语单词）_百度百科</a></h2>
    <div class="b_caption">
      <p>Model 是源自英语的多功能词汇，发音为英 [ˈmɒd (ə)l]。</p>
    </div>
  </li>
</ol>
"""
"""模拟「查询被 Bing 判成词典意图」的跑偏结果页。"""


def test_retries_with_shorter_query_when_results_offtopic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果整页跑偏时，用前三个词重试一次并说明。"""
    good = FIXTURE.read_text(encoding="utf-8")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        calls.append(query)
        simplified = query in {"python asyncio tutorial", "python asyncio"}
        return httpx.Response(200, text=good if simplified else OFFTOPIC_HTML)

    _patch_client(monkeypatch, handler)

    result = asyncio.run(
        web_search("python asyncio tutorial for beginners", max_results=2)
    )

    assert calls[0] == "python asyncio tutorial for beginners"
    assert result.executed == "python asyncio tutorial"
    assert "已简化" in result.note
    assert result.results[0].title.startswith("Python asyncio")


def test_no_retry_when_results_areresults_relevant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果相关时不做额外请求。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("q", ""))
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    _patch_client(monkeypatch, handler)

    result = asyncio.run(web_search("python asyncio tutorial", max_results=2))

    assert len(calls) == 1
    assert result.executed == "python asyncio tutorial"
    assert result.note == ""


def test_keywords_extracts_latin_and_cjk_tokens() -> None:
    """关键词抽取：保留技术 token（含斜杠），丢掉停用词与短词。"""
    assert query_keywords("Model Context Protocol JSON-RPC tools/call") == [
        "model",
        "context",
        "protocol",
        "json-rpc",
        "tools/call",
    ]
    assert "教程" not in query_keywords("python asyncio 教程")
    assert query_keywords("  AI  ") == ["ai"]


def test_relevance_needs_half_ofquery_keywords() -> None:
    """相关性：前三名覆盖一半以上关键词才算相关。"""
    hits = [
        search.SearchHit(
            rank=1,
            title="Python asyncio 模块",
            url="https://example.com/a",
            domain="example.com",
            snippet="asyncio 是标准库",
        )
    ]

    assert results_relevant(hits, ["python", "asyncio"]) is True
    # 4 个关键词命中 2 个 = 刚好一半，仍算相关
    assert (
        results_relevant(hits, ["python", "asyncio", "tutorial", "beginners"]) is True
    )
    # 5 个关键词命中 2 个就不够了
    assert (
        results_relevant(hits, ["python", "asyncio", "tutorial", "beginners", "guide2"])
        is False
    )
    assert results_relevant(hits, []) is True
