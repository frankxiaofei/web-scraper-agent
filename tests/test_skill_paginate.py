"""skill_paginate / page_number URL 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.crawl_rules import build_page_number_url
from src.web.crawl_agent_skills import (
    close_skill_session,
    fetch_list_page_async,
    paginate_async,
    run_async,
)

TJ_SITE = "中国铁道建筑集团有限公司_物资采购网"
ENTRY = "https://tjbid.dlzb.com/v1/"


def test_build_page_number_url_path_suffix():
    assert build_page_number_url(ENTRY, 1, url_style="path_suffix") == ENTRY
    assert (
        build_page_number_url(ENTRY, 2, url_style="path_suffix")
        == "https://tjbid.dlzb.com/v1/2/"
    )
    assert (
        build_page_number_url("https://tjbid.dlzb.com/v1/2/", 3, url_style="path_suffix")
        == "https://tjbid.dlzb.com/v1/3/"
    )


@pytest.mark.asyncio
async def test_paginate_page_number_no_browser():
    """page_number 策略在无 session 时仅计算 URL，不启动浏览器。"""
    with patch("src.web.crawl_agent_skills._get_or_create_session") as mock_sess:
        mock_sess.side_effect = AssertionError("不应创建浏览器 session")
        result = await paginate_async(
            TJ_SITE,
            current_url=ENTRY,
            page_num=2,
            session_id="unit_no_browser",
        )
    assert result["ok"] is True
    assert result["next_page_url"] == "https://tjbid.dlzb.com/v1/2/"
    assert result["page_num"] == 2
    assert result["pagination_strategy"] == "page_number"


def test_skill_paginate_sse_events():
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    events: list[dict] = []
    mock_result = {
        "ok": True,
        "has_next": True,
        "next_page_url": "https://tjbid.dlzb.com/v1/2/",
        "page_num": 2,
        "list_url": "https://tjbid.dlzb.com/v1/2/",
    }
    with patch("src.web.crawl_agent_tools.run_async", return_value=mock_result):
        executor = CrawlAgentToolExecutor(on_url_crawl=lambda item: events.append(item))
        result, _ = executor.execute(
            "skill_paginate",
            {
                "site_id": TJ_SITE,
                "current_url": ENTRY,
                "page_num": 2,
                "session_id": "sse_test",
            },
        )
    assert result["next_page_url"] == "https://tjbid.dlzb.com/v1/2/"
    assert any(e.get("status") == "paginating" for e in events)
    assert any(e.get("status") == "fetching" for e in events)


@pytest.mark.integration
def test_fetch_and_paginate_same_skill_loop():
    """跨多次 run_async 复用同一 Playwright session，不应挂死。"""
    sid = "integration_loop_test"
    try:
        r1 = run_async(fetch_list_page_async(TJ_SITE, page_num=1, session_id=sid), timeout=90)
        assert r1["ok"] is True
        assert r1["item_count"] > 0

        r2 = run_async(
            paginate_async(TJ_SITE, current_url=r1["list_url"], page_num=2, session_id=sid),
            timeout=90,
        )
        assert r2["ok"] is True
        assert r2["next_page_url"] == "https://tjbid.dlzb.com/v1/2/"

        r3 = run_async(fetch_list_page_async(TJ_SITE, page_num=2, session_id=sid), timeout=90)
        assert r3["ok"] is True
        assert r3["item_count"] > 0
        assert "/v1/2/" in r3["list_url"]
    finally:
        run_async(close_skill_session(TJ_SITE, sid), timeout=30)
