"""公告列表按 publish_date 降序排序。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.data_service import NOTICE_MONGO_SORT, NoticeDataService


@pytest.fixture
def sort_sample_notices(tmp_path: Path):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    docs = [
        {
            "title": "较新发布但较早采集",
            "url": "https://example.com/newer-pub",
            "source_site_id": "crec_bidding",
            "publish_date": today,
            "scraped_at": (now - timedelta(days=2)).isoformat(),
            "content_text": "正文 A",
            "page_type": "detail",
        },
        {
            "title": "较早发布但刚采集",
            "url": "https://example.com/older-pub",
            "source_site_id": "crec_bidding",
            "publish_date": yesterday,
            "scraped_at": now.isoformat(),
            "content_text": "正文 B",
            "page_type": "detail",
        },
        {
            "title": "无发布时间回退采集",
            "url": "https://example.com/no-pub",
            "source_site_id": "crec_bidding",
            "scraped_at": (now - timedelta(days=3)).isoformat(),
            "content_text": "正文 C",
            "page_type": "detail",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_list_notices_jsonl_sorted_by_publish_date(sort_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sort_sample_notices.parent
        mock_sites.return_value = [{"id": "crec_bidding", "name": "鲁班商务网"}]

        svc = NoticeDataService()
        result = svc.list_notices(page=1, per_page=10)

    urls = [item["detail_url"] for item in result["items"]]
    assert urls[0] == "https://example.com/newer-pub"
    assert urls[1] == "https://example.com/older-pub"
    assert urls[2] == "https://example.com/no-pub"


def test_list_notices_mongo_uses_publish_date_sort():
    mock_coll = MagicMock()
    mock_cursor = MagicMock()
    mock_coll.find.return_value.sort.return_value.skip.return_value.limit.return_value = iter([])
    mock_coll.count_documents.return_value = 0

    svc = NoticeDataService.__new__(NoticeDataService)
    svc._mongo_available = True
    svc._mongo_coll = mock_coll
    svc._jsonl_cache = None
    svc._jsonl_mtime = 0.0
    svc._enabled_site_ids_cache = ["crec_bidding"]
    svc._enabled_site_ids_mtime = 0.0

    svc.list_notices(page=1, per_page=5)

    mock_coll.find.return_value.sort.assert_called_once_with(NOTICE_MONGO_SORT)


def test_load_jsonl_reloads_when_file_mtime_changes(tmp_path: Path):
    jsonl_path = tmp_path / "notices.jsonl"
    doc_a = {
        "title": "第一条",
        "url": "https://example.com/a",
        "source_site_id": "crec_bidding",
        "publish_date": "2026-07-10",
        "content_text": "正文",
        "page_type": "detail",
    }
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(doc_a, ensure_ascii=False) + "\n")

    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = tmp_path
        mock_sites.return_value = [{"id": "crec_bidding", "name": "鲁班商务网"}]

        svc = NoticeDataService()
        assert svc.list_notices(page=1, per_page=10)["total"] == 1

        doc_b = {
            "title": "第二条",
            "url": "https://example.com/b",
            "source_site_id": "crec_bidding",
            "publish_date": "2026-07-11",
            "content_text": "正文",
            "page_type": "detail",
        }
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(doc_b, ensure_ascii=False) + "\n")

        assert svc.list_notices(page=1, per_page=10)["total"] == 2
