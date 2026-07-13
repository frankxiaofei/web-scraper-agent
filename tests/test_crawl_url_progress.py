"""URL 爬取进度增量提取测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.web.crawl_agent_chat_service import CrawlAgentChatService, _url_crawl_event
from src.web.crawl_agent_tools import CrawlAgentToolExecutor
from src.web.crawl_url_progress import UrlCrawlTracker

TJ_SITE = "中国铁道建筑集团有限公司_物资采购网"


def test_tracker_incremental_fetch_detail():
    tracker = UrlCrawlTracker()
    events = [
        {
            "type": "fetch_detail",
            "message": "抓取详情页 (1/3)",
            "timestamp": "2026-06-17T10:00:01+00:00",
            "data": {"url": "https://example.com/a"},
        },
        {
            "type": "fetch_detail",
            "message": "抓取详情页 (2/3)",
            "timestamp": "2026-06-17T10:00:02+00:00",
            "data": {"url": "https://example.com/b"},
        },
    ]
    batch1 = tracker.process_events(events[:1])
    batch2 = tracker.process_events(events[1:])

    assert len(batch1) == 1
    assert batch1[0]["url"] == "https://example.com/a"
    assert batch1[0]["status"] == "fetching"
    assert batch1[0]["index"] == 1
    assert batch1[0]["page"] == 1

    assert len(batch2) == 1
    assert batch2[0]["index"] == 2
    assert tracker.summary()["total"] == 2


def test_tracker_save_updates_status():
    tracker = UrlCrawlTracker()
    tracker.process_events(
        [
            {
                "type": "fetch_detail",
                "message": "抓取详情页 (1/1)",
                "data": {"url": "https://example.com/x"},
            }
        ]
    )
    saved = tracker.process_events(
        [
            {
                "type": "save",
                "message": "入库: 新增 1, 更新 0",
                "timestamp": "2026-06-17T10:01:00+00:00",
                "data": {
                    "urls": [{"url": "https://example.com/x", "title": "公告标题"}],
                },
            }
        ]
    )
    assert len(saved) == 1
    assert saved[0]["status"] == "saved"
    assert saved[0]["title"] == "公告标题"


def test_tracker_parse_batch_without_detail():
    tracker = UrlCrawlTracker()
    items = tracker.process_events(
        [
            {
                "type": "parse",
                "message": "解析完成: 2 条公告",
                "data": {
                    "urls": [
                        {"url": "https://a.test/1", "title": "A1"},
                        {"url": "https://a.test/2", "title": "A2"},
                    ]
                },
            }
        ]
    )
    assert len(items) == 2
    assert all(i["status"] == "saved" for i in items)


def test_poll_emits_url_crawl_callbacks():
    url_items: list[dict] = []

    def on_url(item: dict) -> None:
        url_items.append(item)

    status_sequence = [
        {
            "ok": True,
            "is_running": True,
            "sync_status": "syncing",
            "run_id": "run-abc",
            "progress": {"run_id": "run-abc", "progress_items": 1, "max_items": 3},
        },
        {
            "ok": True,
            "is_running": False,
            "sync_status": "completed",
            "run_id": "run-abc",
            "progress": {"run_id": "run-abc", "progress_items": 2, "max_items": 3},
        },
    ]

    mock_events = [
        [
            {
                "type": "fetch_detail",
                "message": "抓取详情页 (1/2)",
                "data": {"url": "https://x.test/1"},
            }
        ],
        [
            {
                "type": "fetch_detail",
                "message": "抓取详情页 (1/2)",
                "data": {"url": "https://x.test/1"},
            },
            {
                "type": "fetch_detail",
                "message": "抓取详情页 (2/2)",
                "data": {"url": "https://x.test/2"},
            },
            {
                "type": "save",
                "message": "入库",
                "data": {
                    "urls": [
                        {"url": "https://x.test/1", "title": "T1"},
                        {"url": "https://x.test/2", "title": "T2"},
                    ]
                },
            },
        ],
    ]

    with patch("src.web.crawl_agent_tools.get_sync_service") as mock_sync, patch(
        "src.web.crawl_agent_tools.get_task_service"
    ) as mock_task, patch("src.web.crawl_agent_tools.get_crawl_run_store") as mock_store:
        sync_svc = mock_sync.return_value
        sync_svc.get_sync_status.side_effect = status_sequence
        mock_task.return_value.get_site.return_value = {
            "site": {"site_id": TJ_SITE, "status": "completed", "last_run_status": "success"}
        }
        store = mock_store.return_value
        store.list_events.side_effect = mock_events

        executor = CrawlAgentToolExecutor(on_url_crawl=on_url, sleep_fn=lambda _: None)
        result, _ = executor.execute(
            "crawl_poll_until_done",
            {"site_id": TJ_SITE, "timeout_seconds": 10, "poll_interval": 1},
        )

    assert result["ok"] is True
    assert len(url_items) >= 2
    urls = {u["url"] for u in url_items}
    assert "https://x.test/1" in urls
    assert "https://x.test/2" in urls
    assert result.get("url_summary", {}).get("total") == 2


@pytest.mark.skip(reason="CrawlAgentChatService 已委托 Hermes Agent，本地 LLM fallback SOP 已移除")
@pytest.mark.asyncio
async def test_fallback_sop_yields_url_crawl_via_poll():
    mock_llm = MagicMock()
    mock_llm.available = False

    status_sequence = [
        {
            "ok": True,
            "is_running": True,
            "sync_status": "syncing",
            "run_id": "run-sop",
            "progress": {"run_id": "run-sop", "progress_items": 1, "max_items": 2},
        },
        {
            "ok": True,
            "is_running": False,
            "sync_status": "completed",
            "run_id": "run-sop",
            "progress": {},
        },
    ]
    event_batches = [
        [{"type": "fetch_detail", "message": "抓取详情页 (1/1)", "data": {"url": "https://sop.test/u"}}],
        [
            {"type": "fetch_detail", "message": "抓取详情页 (1/1)", "data": {"url": "https://sop.test/u"}},
            {
                "type": "save",
                "message": "入库",
                "data": {"urls": [{"url": "https://sop.test/u", "title": "SOP"}]},
            },
        ],
    ]

    with patch("src.web.crawl_agent_tools.get_task_service") as mock_task, patch(
        "src.web.crawl_agent_tools.get_sync_service"
    ) as mock_sync, patch("src.web.crawl_agent_tools.get_crawl_run_store") as mock_store, patch(
        "src.web.crawl_agent_tools.get_site_by_id"
    ) as mock_site:
        mock_site.return_value = {"name": "物资采购网", "url": "https://tjbid.dlzb.com/"}
        mock_task.return_value.list_sites.return_value = {"sites": []}
        mock_task.return_value.get_site.return_value = {
            "site": {"site_id": TJ_SITE, "is_running": False, "sync_stale": False, "status": "completed"}
        }
        mock_sync.return_value.get_sync_status.side_effect = status_sequence
        mock_sync.return_value.trigger_sync = MagicMock()
        mock_store.return_value.list_events.side_effect = event_batches

        service = CrawlAgentChatService(llm=mock_llm)
        events = []
        async for ev in service.stream_chat("帮我把 tjbid 全部爬下来"):
            events.append(ev)

    url_events = [e for e in events if e.type == "url_crawl"]
    assert url_events
    assert url_events[0].data.get("url") == "https://sop.test/u"


def test_url_crawl_event_sse_shape():
    ev = _url_crawl_event(
        {"url": "https://a.test", "title": "标题", "status": "fetching", "index": 3}
    )
    d = ev.to_sse_dict()
    assert d["type"] == "url_crawl"
    assert d["data"]["url"] == "https://a.test"
    assert "fetching" in d["summary"]
