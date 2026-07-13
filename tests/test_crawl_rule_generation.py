"""crawl_rules 自动生成相关工具单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.core.dom_selector_hints import analyze_list_dom, format_page_hints_for_llm
from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS

TABLE_HTML = """
<html><head><title>招标公告</title></head><body>
<table class="list-table">
<tr><th>标题</th><th>日期</th></tr>
<tr><td><a href="/notice/1">公告一</a></td><td>2026-01-01</td></tr>
<tr><td><a href="/notice/2">公告二</a></td><td>2026-01-02</td></tr>
<tr><td><a href="/notice/3">公告三</a></td><td>2026-01-03</td></tr>
</table>
</body></html>
"""


def test_tool_schemas_include_rule_generation_tools():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    assert "crawl_test" in names
    assert "crawl_enable_site" in names
    assert "crawl_schedule_status" in names
    assert "skill_analyze_list_dom" in names
    assert "crawl_generate_rule" in names


def test_analyze_list_dom_table():
    result = analyze_list_dom(TABLE_HTML, url="https://example.com/list")
    assert result["ok"] is True
    assert result["selectors"]["container"] == "table"
    assert result["selectors"]["item"] == "tr"
    assert len(result["sample_items"]) >= 1
    hints = format_page_hints_for_llm(result, extra_notes="测试备注")
    assert "table" in hints
    assert "测试备注" in hints


def test_skill_analyze_list_dom_with_html():
    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "skill_analyze_list_dom",
        {"html": TABLE_HTML, "url": "https://example.com/list"},
    )
    assert result["ok"] is True
    assert result["page_hints"]
    assert "table" in summary or "推断" in summary


def test_crawl_test_mock_dry_run():
    executor = CrawlAgentToolExecutor()
    with patch(
        "src.core.crawl_rule_dry_run.dry_run_crawl_rule",
        new=AsyncMock(
            return_value={
                "success": True,
                "items": [{"title": "测试", "url": "https://x/1"}],
                "errors": [],
                "transport": "browser",
            }
        ),
    ):
        result, summary = executor.execute(
            "crawl_test",
            {"site_id": "tjbid", "max_items": 3},
        )
    assert result["ok"] is True
    assert result["item_count"] == 1
    assert "试跑通过" in summary


def test_crawl_test_failure_summary():
    executor = CrawlAgentToolExecutor()
    with patch(
        "src.core.crawl_rule_dry_run.dry_run_crawl_rule",
        new=AsyncMock(return_value={"success": False, "items": [], "errors": ["选择器超时"]}),
    ):
        result, summary = executor.execute("crawl_test", {"site_id": "stub"})
    assert result["ok"] is False
    assert "试跑失败" in summary


def test_crawl_generate_rule_passes_page_hints():
    executor = CrawlAgentToolExecutor()
    mock_gen = AsyncMock(
        return_value={"yaml": "version: 1", "valid": True, "errors": [], "preview": None}
    )
    with patch("src.web.crawl_agent_tools.get_rule_generator") as mock_factory:
        mock_factory.return_value.generate = mock_gen
        executor.execute(
            "crawl_generate_rule",
            {
                "site_id": "demo",
                "url": "https://example.com",
                "page_hints": "container: .list",
            },
        )
    mock_gen.assert_called_once()
    assert mock_gen.call_args.kwargs.get("page_hints") == "container: .list"
    assert mock_gen.call_args.kwargs.get("fetch_hints") is False
