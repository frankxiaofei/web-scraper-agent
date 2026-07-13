"""RuleExecutor 单元测试（mock 浏览器，不测真实站点）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rules import CrawlRule, ListPage, ListPageApi, Pagination
from src.core.rule_executor import RuleExecutor, _get_nested
from src.core.models import NoticeCategory


class TestGetNested(unittest.TestCase):
    def test_dot_path(self) -> None:
        data = {"data": {"records": [{"title": "a"}]}}
        self.assertEqual(_get_nested(data, "data.records"), [{"title": "a"}])

    def test_missing_path(self) -> None:
        self.assertIsNone(_get_nested({"a": 1}, "b.c"))


class TestApiItemsToNotices(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = RuleExecutor(MagicMock(), {"id": "test", "name": "测试站"})

    def test_url_template(self) -> None:
        api = ListPageApi(
            url="/api/list",
            fields={"title": "title", "id": "id", "date": "addtimeStr"},
            url_template="https://example.com/info/{id}",
        )
        rule = CrawlRule(site_id="test", entry_url="https://example.com/")
        items = [{"title": "招标公告A", "id": "123", "addtimeStr": "2026-01-02"}]
        notices = self.executor._api_items_to_notices(items, api, rule)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].title, "招标公告A")
        self.assertEqual(notices[0].url, "https://example.com/info/123")
        self.assertEqual(notices[0].category, NoticeCategory.TENDER)

    def test_win_category(self) -> None:
        api = ListPageApi(url="/api", fields={"title": "title", "url": "url"})
        rule = CrawlRule(site_id="test")
        items = [{"title": "中标结果公示", "url": "https://example.com/w/1"}]
        notices = self.executor._api_items_to_notices(items, api, rule)
        self.assertEqual(notices[0].category, NoticeCategory.WIN)


class TestDomParse(unittest.IsolatedAsyncioTestCase):
    async def test_parse_dom_list(self) -> None:
        executor = RuleExecutor(MagicMock(), {"id": "crec_bidding", "name": "鲁班"})
        page = MagicMock()
        page.url = "https://eproport.crecgec.com/#/notice/notice?noticeType=1"
        page.evaluate = AsyncMock(
            return_value=[
                {
                    "title": "某项目招标公告",
                    "href": "#/notice/noticeView?pubId=abc",
                    "dateText": "2026-06-01",
                }
            ]
        )
        list_page = ListPage(
            container="nz-table",
            item="tr",
            title="a",
            link="a",
            date="td:nth-of-type(4)",
        )
        rule = CrawlRule(
            site_id="crec_bidding",
            entry_url="https://eproport.crecgec.com/#/notice/notice?noticeType=1",
        )
        notices = await executor._parse_dom_list(page, list_page, rule)
        self.assertEqual(len(notices), 1)
        self.assertIn("pubId=abc", notices[0].url)
        self.assertEqual(notices[0].source_site_id, "crec_bidding")


class TestExecuteEntrySteps(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_emits_events(self) -> None:
        browser = MagicMock()
        page = AsyncMock()
        page.url = "about:blank"
        browser.new_page.return_value.__aenter__ = AsyncMock(return_value=page)
        browser.new_page.return_value.__aexit__ = AsyncMock(return_value=False)

        executor = RuleExecutor(browser, {"id": "t", "name": "T", "url": "https://x.com"})
        rule = CrawlRule(
            site_id="t",
            entry_url="https://x.com/list",
            entry_steps=[],
            list_page=ListPage(strategy="dom", container="ul", item="li", title="a", link="a"),
        )

        with patch("src.core.rule_executor.emit_crawl_event") as emit:
            page.evaluate = AsyncMock(return_value=[])
            page.wait_for_selector = AsyncMock()
            await executor.execute(rule, max_items=5)

        types = [c.args[0] for c in emit.call_args_list]
        self.assertIn("fetch_list", types)
        self.assertIn("parse", types)


if __name__ == "__main__":
    unittest.main()
