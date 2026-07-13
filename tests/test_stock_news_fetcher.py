"""stock_news_fetcher AKShare 单元测试（mock，不依赖外网）。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.stock_news_fetcher import StockNewsFetcher, _parse_cn_datetime


class _FakeDataFrame:
    def __init__(self, rows: list[dict], columns: list[str]) -> None:
        self._rows = rows
        self.columns = columns

    def head(self, n: int) -> "_FakeDataFrame":
        return _FakeDataFrame(self._rows[:n], self.columns)

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, row


def test_parse_cn_datetime():
    dt = _parse_cn_datetime("2024-06-22 10:30:00")
    assert dt == datetime(2024, 6, 22, 10, 30, 0)


def test_fetch_akshare_global_news(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "akshare")
    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")

    fake_df = _FakeDataFrame(
        [
            {
                "标题": "新能源政策加码，储能产业链受益",
                "摘要": "多地出台储能补贴细则",
                "发布时间": "2024-06-22 09:00:00",
                "链接": "https://example.com/news/1",
            },
            {
                "标题": "半导体设备国产化加速",
                "摘要": "先进封装产能扩张",
                "发布时间": "2024-06-22 08:00:00",
                "链接": "https://example.com/news/2",
            },
        ],
        ["标题", "摘要", "发布时间", "链接"],
    )

    mock_ak = MagicMock()
    mock_ak.stock_info_global_em.return_value = fake_df

    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    with patch("src.core.stock_news_fetcher.is_akshare_available", return_value=True):
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            fetcher = StockNewsFetcher()
            items = fetcher.fetch_akshare()

    assert len(items) == 2
    assert items[0]["source_id"] == "akshare"
    assert "新能源" in items[0]["industries"]
    mod._fetcher = None


def test_fetch_akshare_degrades_to_watch_symbols(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "akshare")
    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")

    empty_df = _FakeDataFrame([], ["标题"])
    symbol_df = _FakeDataFrame(
        [{"新闻标题": "贵州茅台发布年报", "发布时间": "2024-06-22", "新闻链接": "https://x.com/1", "新闻内容": "业绩稳健"}],
        ["新闻标题", "发布时间", "新闻链接", "新闻内容"],
    )

    mock_ak = MagicMock()
    mock_ak.stock_info_global_em.return_value = empty_df
    mock_ak.stock_news_em.return_value = symbol_df

    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    with patch("src.core.stock_news_fetcher.is_akshare_available", return_value=True):
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            fetcher = StockNewsFetcher()
            items = fetcher.fetch_akshare()

    assert len(items) >= 1
    assert items[0]["origin"] == "akshare"
    mod._fetcher = None


def test_fetch_akshare_network_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "akshare")

    mock_ak = MagicMock()
    mock_ak.stock_info_global_em.side_effect = ConnectionError("network timeout")

    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    with patch("src.core.stock_news_fetcher.is_akshare_available", return_value=True):
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            fetcher = StockNewsFetcher()
            items = fetcher.fetch_akshare()

    assert items == []
    mod._fetcher = None


def test_fetch_bid_notices_uses_enabled_site_ids(monkeypatch):
    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    mock_svc = MagicMock()
    mock_svc.data_source = "mongodb"
    mock_svc._mongo_coll = MagicMock()
    mock_svc._enabled_site_ids.return_value = ["site_a", "site_b"]
    mock_svc._mvp_source_filter_mongo.return_value = {"source_site_id": {"$in": ["site_a", "site_b"]}}
    mock_svc._mongo_coll.find.return_value.sort.return_value = iter([])

    fetcher = StockNewsFetcher()
    with patch("src.web.data_service.get_data_service", return_value=mock_svc):
        items = fetcher.fetch_bid_notices(days=7)

    assert items == []
    mock_svc._enabled_site_ids.assert_called_once()
    mock_svc._mvp_source_filter_mongo.assert_called_once_with(["site_a", "site_b"])
    mod._fetcher = None


def test_fetch_all_marks_degraded_when_akshare_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "akshare")
    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")

    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    fetcher = StockNewsFetcher()
    with patch.object(fetcher, "fetch_akshare", return_value=[]):
        result = fetcher.fetch_all(days=7)

    assert result["ok"] is True
    assert result.get("degraded") is True
    mod._fetcher = None
