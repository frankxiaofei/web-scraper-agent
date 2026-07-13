"""爬取链接提取测试。"""

from src.web.crawled_links import extract_links_from_events, get_crawled_links


def test_extract_links_from_save_event():
    events = [
        {
            "type": "save",
            "data": {
                "urls": [
                    {"title": "公告A", "url": "https://example.com/a"},
                    {"title": "公告B", "url": "https://example.com/b"},
                ]
            },
        }
    ]
    links = extract_links_from_events(events)
    assert len(links) == 2
    assert links[0]["title"] == "公告A"


def test_extract_links_from_fetch_detail_events():
    events = [
        {"type": "fetch_detail", "data": {"url": "https://example.com/1", "title": "详情1"}},
        {"type": "fetch_detail", "data": {"url": "https://example.com/2"}},
    ]
    links = extract_links_from_events(events)
    assert len(links) == 2
    assert links[1]["title"] == "https://example.com/2"


def test_get_crawled_links_prefers_events():
    run = {"site_id": "test", "notices_count": 1}
    events = [
        {
            "type": "parse",
            "data": {"urls": [{"title": "来自事件", "url": "https://example.com/event"}]},
        }
    ]
    links = get_crawled_links(run, events)
    assert len(links) == 1
    assert links[0]["title"] == "来自事件"
