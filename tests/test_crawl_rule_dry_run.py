"""crawl rule dry-run API 逻辑单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rule_dry_run import dry_run_crawl_rule, _prepare_rule_for_dry_run
from src.core.crawl_rules import CrawlRule, CrawlLimits, ListPage, Pagination


def test_prepare_rule_for_dry_run_caps_pages_and_items():
    rule = CrawlRule(
        site_id="test",
        list_page=ListPage(container="table"),
        pagination=Pagination(max_pages=10),
        limits=CrawlLimits(max_pages=10, max_items=100),
    )
    capped = _prepare_rule_for_dry_run(rule, max_items=5, max_pages=1)
    assert capped.limits.max_pages == 1
    assert capped.limits.max_items == 5
    assert capped.pagination is not None
    assert capped.pagination.max_pages == 1


@pytest.mark.asyncio
async def test_dry_run_unknown_site():
    result = await dry_run_crawl_rule("nonexistent_site_id_xyz")
    assert result["success"] is False
    assert result["items"] == []
    assert "站点不存在" in result["errors"][0]


@pytest.mark.asyncio
async def test_dry_run_invalid_yaml():
    site_id = "中国电力建设集团有限公司_公共资源交易服务平台"
    result = await dry_run_crawl_rule(site_id, yaml_text="not: valid: yaml: [")
    assert result["success"] is False
    assert result["items"] == []
    assert result["errors"]


@pytest.mark.asyncio
async def test_dry_run_success_returns_preview_items():
    site_id = "test_site"
    site = {"id": site_id, "name": "测试", "url": "https://example.com"}
    rule = CrawlRule(site_id=site_id, list_page=ListPage(container="table"))

    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()

    from src.core.models import BidNotice, NoticeCategory

    notice = BidNotice(
        title="测试公告",
        url="https://example.com/n/1",
        source_site_id=site_id,
        source_site_name="测试",
        source_url="https://example.com",
        category=NoticeCategory.TENDER,
    )

    with patch("src.core.crawl_rule_dry_run.get_site_by_id", return_value=site), patch(
        "src.core.crawl_rule_dry_run.load_crawl_rule", return_value=rule
    ), patch("src.core.crawl_rule_dry_run.BrowserPool", return_value=mock_pool), patch(
        "src.core.crawl_rule_dry_run.RuleExecutor"
    ) as mock_executor_cls:
        mock_executor_cls.return_value.execute = AsyncMock(return_value=[notice])
        result = await dry_run_crawl_rule(site_id, max_pages=1, max_items=5)

    assert result["success"] is True
    assert result["errors"] == []
    assert len(result["items"]) == 1
    assert result["items"][0]["title"] == "测试公告"
    assert result["items"][0]["url"] == "https://example.com/n/1"
