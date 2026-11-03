"""text_stats 工具测试。"""

from __future__ import annotations

from blue_service_text_stats import text_stats


def test_text_stats_counts() -> None:
    """字符、词与行计数符合空白切分语义。"""
    result = text_stats("hello world\n第二行\n")
    assert result.characters == len("hello world\n第二行\n")
    assert result.words == 3
    assert result.lines == 2
