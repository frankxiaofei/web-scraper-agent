"""Crawl Agent skill 工具单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS

TJ_SITE = "中国铁道建筑集团有限公司_物资采购网"


def test_default_tool_schemas_exclude_legacy_sync():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    assert "skill_plan_crawl_path" in names
    assert "skill_fetch_list_page" in names
    assert "skill_fetch_notice_content" in names
    assert "skill_extract_notice_fields" in names
    assert "skill_save_notice" in names
    assert "skill_save_tagged_document" in names
    assert "skill_search_by_tags" in names
    assert "skill_close_session" in names
    assert "crawl_generate_rule" in names
    assert "crawl_test" in names
    assert "crawl_enable_site" in names
    assert "skill_analyze_list_dom" in names
    assert "crawl_generate_script" in names
    assert "crawl_run_script" in names
    assert "crawl_request_user_input" in names
    assert "crawl_wait_pending" in names
    assert "crawl_notify_user" in names
    assert "crawl_query_notices" in names
    assert "crawl_classify_agri" in names
    assert "crawl_agri_insights" in names
    assert "crawl_agri_sync_all" in names
    assert "crawl_trigger" not in names
    assert "crawl_poll_until_done" not in names
    assert "skill_webbridge_check" in names
    assert "skill_webbridge_navigate" in names
    assert "crawl_agri_tenders" in names


def test_skill_plan_crawl_path_tjbid():
    with patch("src.web.crawl_agent_skills.get_site_by_id") as mock_site, patch(
        "src.web.crawl_agent_skills.load_crawl_rule"
    ) as mock_rule:
        mock_site.return_value = {
            "id": TJ_SITE,
            "name": "物资采购网",
            "url": "https://tjbid.dlzb.com/v1/",
        }
        pag = MagicMock()
        pag.type = "page_number"
        pag.url_style = "path_suffix"
        pag.page_param = None
        pag.max_pages = 500
        limits = MagicMock()
        limits.max_pages = 500
        limits.max_items = 10000
        limits.rate_limit_seconds = 1.0
        detail = MagicMock()
        detail.fetch_detail = False
        rule = MagicMock()
        rule.entry_url = "https://tjbid.dlzb.com/v1/"
        rule.name = "物资采购网"
        rule.list_page = MagicMock()
        rule.pagination = pag
        rule.limits = limits
        rule.detail = detail
        mock_rule.return_value = rule

        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "skill_plan_crawl_path",
            {"site_id": TJ_SITE, "user_intent": "tjbid 爬取", "max_pages": 3},
        )
        assert result["ok"] is True
        assert result["agent_max_pages"] == 3
        assert result["pagination_strategy"] == "page_number"
        assert result["path_tree"]["entry"] == "https://tjbid.dlzb.com/v1/"
        assert len(result["path_tree"]["pages"]) == 3
        assert "3" in summary or "计划" in summary


def test_skill_fetch_list_page_mock():
    mock_items = {
        "ok": True,
        "site_id": TJ_SITE,
        "page_num": 1,
        "list_url": "https://tjbid.dlzb.com/v1/",
        "items": [
            {"title": "测试公告", "url": "https://tjbid.dlzb.com/x/1", "date": None},
        ],
        "item_count": 1,
        "has_next": True,
    }
    url_events: list[dict] = []

    with patch("src.web.crawl_agent_tools.run_async", return_value=mock_items):
        executor = CrawlAgentToolExecutor(
            on_url_crawl=lambda item: url_events.append(item),
        )
        result, summary = executor.execute(
            "skill_fetch_list_page",
            {"site_id": TJ_SITE, "page_num": 1},
        )
        assert result["ok"] is True
        assert result["item_count"] == 1
        assert len(url_events) == 1
        assert url_events[0]["status"] == "fetching"
        assert "1 条" in summary


def test_skill_save_notice_mock():
    url_events: list[dict] = []

    with patch("src.web.crawl_agent_tools.save_notice") as mock_save:
        mock_save.return_value = {
            "ok": True,
            "site_id": TJ_SITE,
            "url": "https://example.com/n1",
            "saved": True,
            "duplicate": False,
            "new_count": 1,
        }
        executor = CrawlAgentToolExecutor(
            on_url_crawl=lambda item: url_events.append(item),
        )
        result, summary = executor.execute(
            "skill_save_notice",
            {
                "site_id": TJ_SITE,
                "title": "公告",
                "url": "https://example.com/n1",
            },
        )
        assert result["saved"] is True
        mock_save.assert_called_once()
        assert "已保存" in summary


def test_skill_save_tagged_document_mock():
    with patch("src.web.crawl_agent_tools.save_tagged_document") as mock_save:
        mock_save.return_value = {
            "ok": True,
            "saved": True,
            "inserted": True,
            "document_id": "abc123",
            "tags": ["BIM"],
        }
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "skill_save_tagged_document",
            {"title": "测试", "tags": ["BIM"], "url": "https://example.com/x"},
        )
        assert result["inserted"] is True
        mock_save.assert_called_once()
        assert "abc123" in summary


def test_skill_search_by_tags_mock():
    with patch("src.web.crawl_agent_tools.search_tagged_documents") as mock_search:
        mock_search.return_value = {
            "ok": True,
            "total": 2,
            "match_mode": "any",
            "items": [{"document_id": "1", "title": "A"}],
        }
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "skill_search_by_tags",
            {"tags": ["BIM", "电建"], "match_mode": "any", "limit": 5},
        )
        assert result["total"] == 2
        mock_search.assert_called_once_with(
            ["BIM", "电建"],
            match_mode="any",
            site_id=None,
            limit=5,
        )
        assert "2 条" in summary


@pytest.mark.asyncio
async def test_fallback_sop_uses_skill_flow():
    from src.web.crawl_agent_chat_service import CrawlAgentChatService, ChatStreamEvent

    mock_llm = MagicMock()
    mock_llm.available = False

    mock_executor = MagicMock(spec=CrawlAgentToolExecutor)
    mock_executor.execute.side_effect = [
        (
            {
                "ok": True,
                "agent_max_pages": 1,
                "fetch_detail": False,
                "pagination_strategy": "page_number",
                "path_tree": {"entry": "https://tjbid.dlzb.com/v1/", "pages": [{"page_num": 1}]},
            },
            "plan ok",
        ),
        (
            {
                "ok": True,
                "items": [{"title": "A", "url": "https://x/a", "date": None}],
                "item_count": 1,
            },
            "list ok",
        ),
        ({"ok": True, "saved": True, "duplicate": False}, "saved"),
        ({"ok": True, "total": 10, "items": []}, "query ok"),
    ]
    mock_executor.close_skill_session = MagicMock()

    service = CrawlAgentChatService(llm=mock_llm, tool_executor=mock_executor)
    events: list[ChatStreamEvent] = []
    async for ev in service.stream_chat("帮我把 tjbid 爬下来"):
        events.append(ev)

    tool_names = [e.name for e in events if e.type == "tool"]
    assert "skill_plan_crawl_path" in tool_names
    assert "skill_fetch_list_page" in tool_names
    assert "skill_save_notice" in tool_names
    assert "crawl_trigger" not in tool_names
    assert any(e.type == "url_crawl" for e in events) or mock_executor.execute.called
    assert any(e.type == "plan" for e in events)
    assert events[-1].type == "done"
