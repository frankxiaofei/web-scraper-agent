"""policy_news_fetcher 单元测试（mock，不依赖外网）。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.policy_news_fetcher import PolicyNewsFetcher


def test_fetch_mock_items():
    import src.core.policy_news_fetcher as mod

    mod._fetcher = None
    fetcher = PolicyNewsFetcher()
    items = fetcher.fetch_mock()
    assert len(items) >= 1
    assert items[0]["source_id"] == "mock"
    assert "十五五" in items[0]["title"] or "十五五" in (items[0].get("keywords") or [])
    mod._fetcher = None


def test_fetch_all_fallback_mock_on_network_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_USE_MOCK_WHEN_EMPTY", "true")
    monkeypatch.setenv(
        "POLICY_NEWS_SOURCES",
        "gov_cn,ndrc,miit,most,moa,mee,sasac,mofcom",
    )
    news_dir = tmp_path / "policy_news"
    monkeypatch.setattr("src.core.policy_news_fetcher.POLICY_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.policy_news_fetcher.POLICY_NEWS_INDEX", news_dir / "index.json")

    import src.core.policy_news_fetcher as mod

    mod._fetcher = None

    class _FailClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            raise ConnectionError("network blocked in test")

    with patch.object(PolicyNewsFetcher, "_http_client", return_value=_FailClient()):
        fetcher = PolicyNewsFetcher()
        result = fetcher.fetch_all(days=30)

    assert result["ok"] is True
    assert result.get("used_mock") or result.get("degraded")
    assert result["deduped"] >= 1
    mod._fetcher = None


def test_list_news_mock_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_USE_MOCK_WHEN_EMPTY", "true")
    news_dir = tmp_path / "policy_news"
    news_dir.mkdir()
    monkeypatch.setattr("src.core.policy_news_fetcher.POLICY_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.policy_news_fetcher.POLICY_NEWS_INDEX", news_dir / "index.json")

    import src.core.policy_news_fetcher as mod

    mod._fetcher = None
    fetcher = PolicyNewsFetcher()
    monkeypatch.setattr(fetcher, "_load_from_mongo", lambda **kwargs: [])
    data = fetcher.list_news(limit=5, days=30)
    assert data["total"] >= 1
    assert data["storage"] == "mock"
    assert data.get("disclaimer")
    mod._fetcher = None


def test_matched_policy_keywords():
    from src.core.policy_sources import matched_policy_keywords, matched_policy_themes

    text = "十五五规划纲要与新质生产力产业政策"
    kws = matched_policy_keywords(text)
    assert "十五五" in kws
    themes = matched_policy_themes(text)
    assert "新质生产力" in themes
