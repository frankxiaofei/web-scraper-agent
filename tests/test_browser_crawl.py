"""浏览器自动爬取 MVP 单元测试。"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from src.web import browser_crawl as bc
from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    bc._last_crawl_at.clear()
    yield
    bc._last_crawl_at.clear()


def test_infer_site_id_from_url():
    sid = bc.infer_site_id_from_url("https://tjbid.dlzb.com/v1/list")
    assert sid == "中国铁道建筑集团有限公司_物资采购网"


def test_infer_site_id_unknown():
    assert bc.infer_site_id_from_url("https://example.com/x") is None


def test_rate_limit_blocks_rapid_calls():
    err1 = bc.check_browser_crawl_rate_limit("sess-a")
    err2 = bc.check_browser_crawl_rate_limit("sess-a")
    assert err1 is None
    assert err2 is not None
    assert "频繁" in err2


def test_resolve_effective_max_pages_caps():
    assert bc._resolve_effective_max_pages(None, 99) == 20
    assert bc._resolve_effective_max_pages(None, 2) == 2


def test_list_extract_params_generic_without_rule():
    selector, hint, mode = bc._list_extract_params(None)
    assert mode == "generic"
    assert selector is None


@patch("src.web.browser_crawl.save_notice")
@patch("src.web.browser_crawl._webbridge_paginate")
@patch("src.web.browser_crawl._extract_items")
@patch("src.web.browser_crawl.webbridge_snapshot")
@patch("src.web.browser_crawl.get_site_by_id")
@patch("src.web.browser_crawl.load_crawl_rule")
def test_browser_auto_crawl_single_page_save(
    mock_rule,
    mock_site,
    mock_snap,
    mock_extract,
    mock_paginate,
    mock_save,
):
    mock_site.return_value = {"id": "tjbid", "name": "铁建", "url": "https://tjbid.dlzb.com/"}
    mock_rule.return_value = None
    mock_snap.return_value = {
        "ok": True,
        "url": "https://tjbid.dlzb.com/v1/",
        "title": "列表",
    }
    mock_extract.return_value = {
        "ok": True,
        "items": [{"title": "公告1", "url": "https://tjbid.dlzb.com/a"}],
        "extraction_mode": "rule",
    }
    mock_save.return_value = {"ok": True, "saved": True, "duplicate": False}

    result = bc.browser_auto_crawl(
        session_id="browser-ui",
        site_id="tjbid",
        max_pages=1,
        save=True,
    )

    assert result["ok"] is True
    assert result["item_count"] == 1
    assert result["saved_count"] == 1
    mock_paginate.assert_not_called()
    mock_save.assert_called_once()


@patch("src.web.browser_crawl.save_notice")
@patch("src.web.browser_crawl._extract_items")
@patch("src.web.browser_crawl.webbridge_snapshot")
@patch("src.web.browser_crawl.get_site_by_id")
@patch("src.web.browser_crawl.load_crawl_rule")
def test_browser_auto_crawl_generic_needs_confirmation(
    mock_rule,
    mock_site,
    mock_snap,
    mock_extract,
    mock_save,
):
    mock_site.return_value = {"id": "demo", "name": "Demo", "url": "https://demo.test/"}
    mock_rule.return_value = None
    mock_snap.return_value = {"ok": True, "url": "https://demo.test/", "title": "x"}
    mock_extract.return_value = {
        "ok": True,
        "items": [{"title": "L1", "url": "https://demo.test/1"}],
        "extraction_mode": "generic",
    }

    result = bc.browser_auto_crawl(
        session_id="browser-ui",
        site_id="demo",
        save=True,
        confirm_generic=False,
    )

    assert result["needs_confirmation"] is True
    assert result["skipped_save"] is True
    mock_save.assert_not_called()


def test_webbridge_extract_links_generic():
    from src.web import webbridge_skills as wb

    healthy = {"ok": True, "available": True}
    items_json = json.dumps(
        {"items": [{"title": "链接", "url": "https://x.com/a"}], "count": 1}
    )

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"value": items_json}).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(
        wb, "_urlopen", side_effect=_urlopen
    ):
        result = wb.webbridge_extract_links_generic(max_items=5)

    assert result["ok"] is True
    assert result["item_count"] == 1
    assert result.get("generic") is True


def test_tool_schema_includes_auto_crawl():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    assert "skill_webbridge_auto_crawl" in names
    assert len(names) == 37


def test_executor_auto_crawl_mock():
    mock_result = {
        "ok": True,
        "item_count": 2,
        "saved_count": 1,
        "needs_confirmation": False,
    }
    with patch("src.web.browser_crawl.browser_auto_crawl", return_value=mock_result):
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "skill_webbridge_auto_crawl",
            {"site_id": "tjbid", "max_pages": 1},
        )
    assert result["ok"] is True
    assert "2" in summary
