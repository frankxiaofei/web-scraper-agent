"""数据列表标签与站点筛选。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.notice_tags import WECHAT_TAG
from src.web.data_service import NoticeDataService


@pytest.fixture
def mixed_notices_jsonl(tmp_path: Path):
    docs = [
        {
            "title": "微信公告",
            "url": "https://mp.weixin.qq.com/s/a",
            "source_site_id": "wechat_lpfb",
            "category": "wechat",
            "wechat": True,
            "tags": [WECHAT_TAG],
            "publish_date": "2026-07-10",
            "content_text": "微信正文",
            "page_type": "detail",
        },
        {
            "title": "央企公告",
            "url": "https://example.com/b",
            "source_site_id": "crec_bidding",
            "publish_date": "2026-07-09",
            "content_text": "央企正文",
            "page_type": "detail",
        },
        {
            "title": "行业资讯",
            "url": "https://example.com/c",
            "source_site_id": "crec_bidding",
            "tags": ["行业资讯"],
            "publish_date": "2026-07-08",
            "content_text": "资讯正文",
            "page_type": "detail",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_list_notices_wechat_tag_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布", "category": "wechat"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        all_result = svc.list_notices(page=1, per_page=10)
        wechat_result = svc.list_notices(page=1, per_page=10, tag=WECHAT_TAG)
        industry_result = svc.list_notices(page=1, per_page=10, tag="行业资讯")

    assert all_result["total"] == 3
    assert wechat_result["total"] == 1
    assert wechat_result["items"][0]["title"] == "微信公告"
    assert WECHAT_TAG in wechat_result["items"][0]["tags"]
    assert industry_result["total"] == 1
    assert industry_result["items"][0]["title"] == "行业资讯"


def test_list_notices_wechat_tag_legacy_jsonl(mixed_notices_jsonl):
    """无 tags 字段的历史微信数据仍可通过标签筛选。"""
    legacy_path = mixed_notices_jsonl.parent / "legacy.jsonl"
    doc = {
        "title": "历史微信",
        "url": "https://mp.weixin.qq.com/s/legacy",
        "source_site_id": "wechat_lpfb",
        "category": "wechat",
        "publish_date": "2026-07-07",
        "content_text": "正文",
        "page_type": "detail",
    }
    with legacy_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = legacy_path.parent
        mock_sites.return_value = [{"id": "wechat_lpfb", "name": "临平发布", "category": "wechat"}]

        svc = NoticeDataService()
        svc._jsonl_path = legacy_path
        svc._jsonl_cache = None
        result = svc.list_notices(page=1, per_page=10, tag=WECHAT_TAG)

    assert result["total"] == 1
    assert result["items"][0]["title"] == "历史微信"


def test_list_tags_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        tags = svc.list_tags()

    assert WECHAT_TAG in tags
    assert "行业资讯" in tags


def test_list_notices_multi_tags_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布", "category": "wechat"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        result = svc.list_notices(page=1, per_page=10, tags=[WECHAT_TAG, "行业资讯"])

    assert result["total"] == 2
    titles = {item["title"] for item in result["items"]}
    assert titles == {"微信公告", "行业资讯"}


def test_list_notices_multi_site_ids_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布", "category": "wechat"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        result = svc.list_notices(page=1, per_page=10, site_ids=["wechat_lpfb"])

    assert result["total"] == 1
    assert result["items"][0]["title"] == "微信公告"


def test_list_notices_keyword_matches_tags_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布", "category": "wechat"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        result = svc.list_notices(page=1, per_page=10, keyword="行业资讯", has_content=False)

    assert result["total"] == 1
    assert result["items"][0]["title"] == "行业资讯"


def test_list_notices_mongo_multi_tags_query():
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.skip.return_value.limit.return_value = iter([])
    mock_coll.count_documents.return_value = 0

    svc = NoticeDataService.__new__(NoticeDataService)
    svc._mongo_available = True
    svc._mongo_coll = mock_coll
    svc._jsonl_cache = None
    svc._enabled_site_ids_cache = ["wechat_lpfb", "crec_bidding"]
    svc._enabled_site_ids_mtime = 0.0

    svc.list_notices(page=1, per_page=5, tags=[WECHAT_TAG, "行业资讯"])

    query = mock_coll.count_documents.call_args[0][0]
    assert "$and" in query
    tag_clause = query["$and"][1]
    assert "$or" in tag_clause
    assert len(tag_clause["$or"]) == 2
    assert {"tags": "行业资讯"} in tag_clause["$or"]
    wechat_branch = tag_clause["$or"][0]
    assert "$or" in wechat_branch
    assert {"tags": WECHAT_TAG} in wechat_branch["$or"]


def test_list_notices_mongo_wechat_tag_query():
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.skip.return_value.limit.return_value = iter([])
    mock_coll.count_documents.return_value = 0

    svc = NoticeDataService.__new__(NoticeDataService)
    svc._mongo_available = True
    svc._mongo_coll = mock_coll
    svc._jsonl_cache = None
    svc._enabled_site_ids_cache = ["wechat_lpfb", "crec_bidding"]
    svc._enabled_site_ids_mtime = 0.0

    svc.list_notices(page=1, per_page=5, tag=WECHAT_TAG)

    query = mock_coll.count_documents.call_args[0][0]
    assert "$and" in query
    tag_clause = query["$and"][1]
    assert "$or" in tag_clause
    assert {"tags": WECHAT_TAG} in tag_clause["$or"]


def test_list_notices_mongo_generic_tag_query():
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.skip.return_value.limit.return_value = iter([])
    mock_coll.count_documents.return_value = 0

    svc = NoticeDataService.__new__(NoticeDataService)
    svc._mongo_available = True
    svc._mongo_coll = mock_coll
    svc._jsonl_cache = None
    svc._enabled_site_ids_cache = ["crec_bidding"]
    svc._enabled_site_ids_mtime = 0.0

    svc.list_notices(page=1, per_page=5, tag="行业资讯")

    query = mock_coll.count_documents.call_args[0][0]
    tag_clause = query["$and"][1]
    assert tag_clause == {"tags": "行业资讯"}


def test_update_notice_tags_jsonl(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        all_items = svc.list_notices(page=1, per_page=10)["items"]
        target = next(item for item in all_items if item["title"] == "央企公告")
        updated = svc.update_notice_tags(target["id"], ["重点项目", "行业资讯"])

    assert updated is not None
    assert "重点项目" in updated["tags"]
    assert "行业资讯" in updated["tags"]

    svc._jsonl_cache = None
    tags = svc.list_tags()
    assert "重点项目" in tags

    filtered = svc.list_notices(page=1, per_page=10, tag="重点项目")
    assert filtered["total"] == 1
    assert filtered["items"][0]["title"] == "央企公告"


def test_update_notice_tags_wechat_keeps_auto_tag(mixed_notices_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings, patch(
        "src.web.data_service.load_enabled_sites"
    ) as mock_sites:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = mixed_notices_jsonl.parent
        mock_sites.return_value = [
            {"id": "wechat_lpfb", "name": "临平发布"},
            {"id": "crec_bidding", "name": "鲁班商务网"},
        ]

        svc = NoticeDataService()
        wechat = next(
            item for item in svc.list_notices(page=1, per_page=10)["items"]
            if item["title"] == "微信公告"
        )
        updated = svc.update_notice_tags(wechat["id"], ["精选"])

    assert updated is not None
    assert WECHAT_TAG in updated["tags"]
    assert "精选" in updated["tags"]


def test_update_notice_tags_mongo():
    mock_coll = MagicMock()
    mock_coll.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
    doc = {
        "_id": "abc123",
        "title": "测试",
        "url": "https://example.com/x",
        "source_site_id": "crec_bidding",
        "content_text": "正文",
        "page_type": "detail",
        "tags": [],
    }

    svc = NoticeDataService.__new__(NoticeDataService)
    svc._mongo_available = True
    svc._mongo_coll = mock_coll
    svc._jsonl_cache = None
    svc._enabled_site_ids_cache = ["crec_bidding"]
    svc._enabled_site_ids_mtime = 0.0
    svc._jsonl_path = Path("/tmp/unused.jsonl")

    with patch.object(svc, "_get_from_mongo", return_value=doc), patch.object(
        svc, "_enabled_site_ids", return_value=["crec_bidding"]
    ), patch.object(svc, "get_notice") as mock_get:
        mock_get.return_value = {"id": "abc123", "tags": ["人工标签"]}
        result = svc.update_notice_tags("abc123", ["人工标签"])

    assert result == {"id": "abc123", "tags": ["人工标签"]}
    update_doc = mock_coll.update_one.call_args[0][1]["$set"]
    assert update_doc["tags"] == ["人工标签"]
