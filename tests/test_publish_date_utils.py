"""publish_date 解析与 backfill 辅助测试。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backfill_publish_date import (  # noqa: E402
    apply_publish_date_update,
    resolve_publish_date,
    should_process_doc,
)
from src.core.powerchina_notice import (  # noqa: E402
    extract_publish_time_from_get_info,
    extract_publish_time_from_row,
)
from src.core.publish_date_utils import (  # noqa: E402
    extract_publish_date_from_content,
    parse_date_from_page_text,
    parse_publish_datetime,
    publish_date_suspicious,
    reconcile_publish_date,
    serialize_publish_date,
)


def test_parse_publish_datetime_iso_and_space():
    assert parse_publish_datetime("2026-06-17T13:45:01").day == 17
    assert parse_publish_datetime("2026-06-17 10:25:01").month == 6
    assert parse_publish_datetime("2026-06-17") is not None


def test_extract_publish_date_from_content():
    text = "公告发布时间为2026年6月24日。投标文件递交截止时间为2026年7月4日17时"
    dt = extract_publish_date_from_content(text)
    assert dt == datetime(2026, 6, 24)


def test_parse_publish_datetime_rejects_signoff_deadline():
    assert parse_publish_datetime("签收登记截止：2026-07-09 18:00:00") is None


def test_parse_publish_datetime_rejects_other_deadline_labels():
    assert parse_publish_datetime("投标截止时间：2026-08-01 09:00:00") is None
    assert parse_publish_datetime("开标时间：2026-07-15 10:00:00") is None


def test_extract_publish_date_rejects_deadline_only_content():
    text = "签收登记截止：2026-07-09 18:00:00"
    assert extract_publish_date_from_content(text) is None


def test_extract_publish_date_prefers_publish_over_deadline():
    text = (
        "签收登记截止：2026-07-09 18:00:00\n"
        "公告发布时间为2026年6月24日"
    )
    assert extract_publish_date_from_content(text) == datetime(2026, 6, 24)


def test_parse_date_from_page_text_prefers_publish_label():
    text = "签收登记截止：2026-07-09 18:00:00\n发布时间：2026-06-20"
    assert parse_date_from_page_text(text) == datetime(2026, 6, 20)


def test_parse_date_from_page_text_rejects_deadline_only():
    assert parse_date_from_page_text("签收登记截止：2026-07-09 18:00:00") is None


def test_ec_ceec_extract_publish_datetime_from_content():
    """能建 ec.ceec 正文「公告发布时间：YYYY-MM-DD HH:MM:SS」。"""
    text = (
        "公告发布时间：2026-07-06 16:37:00\n"
        "签收登记截止：2026-07-09 18:00:00\n"
        "截标/开标时间：2024-11-14 14:00:00"
    )
    dt = extract_publish_date_from_content(text)
    assert dt == datetime(2026, 7, 6, 16, 37, 0)


def test_ec_ceec_list_deadline_date_rejected_without_publish_label():
    """列表页仅含签收截止日时不得当作发布时间。"""
    assert parse_date_from_page_text("签收登记截止：2026-07-09 18:00:00") is None
    assert parse_date_from_page_text("2026-07-09") is None


def test_reconcile_publish_date_prefers_content_over_list_deadline():
    content = (
        "新疆油田输变电项目三标段35kV外引电源专业分包采购招标公告\n"
        "公告发布时间：2026-07-06 16:37:00\n"
        "签收登记截止：2026-07-09 18:00:00"
    )
    resolved = reconcile_publish_date(
        publish_date=datetime(2026, 7, 9),
        content_text=content,
        scraped_at=datetime(2026, 7, 7, 16, 48),
    )
    assert resolved == datetime(2026, 7, 6, 16, 37, 0)


def test_publish_date_suspicious_when_after_scrape_or_conflicts_content():
    doc = {
        "publish_date": "2026-07-09T00:00:00",
        "scraped_at": "2026-07-07T16:48:00",
        "content_text": "公告发布时间：2026-07-06 16:37:00",
    }
    assert publish_date_suspicious(doc) is True
    assert publish_date_suspicious(
        {"publish_date": "2026-07-06T00:00:00", "scraped_at": "2026-07-07T10:00:00",
         "content_text": "公告发布时间：2026-07-06 16:37:00"}
    ) is False


def test_extract_publish_time_from_row_and_get_info():
    row = {"publishTime": "2026-06-22T13:45:01", "id": "2409469974"}
    assert extract_publish_time_from_row(row).day == 22
    info = {"publishTime": "2026-06-17 10:25:01"}
    assert extract_publish_time_from_get_info(info).day == 17


def test_publish_date_suspicious():
    assert publish_date_suspicious({"publish_date": None, "scraped_at": "2026-06-29"}) is True
    assert publish_date_suspicious(
        {"publish_date": "2026-06-29T00:00:00", "scraped_at": "2026-06-29T23:00:00"}
    ) is True
    assert publish_date_suspicious(
        {"publish_date": "2026-06-17T00:00:00", "scraped_at": "2026-07-02T10:00:00"}
    ) is False


def test_serialize_publish_date():
    assert serialize_publish_date(datetime(2026, 6, 17, 15, 30)) == "2026-06-17T00:00:00"


def test_should_process_doc_modes():
    doc = {"publish_date": None}
    assert should_process_doc(doc, only_missing=True, fix_suspicious=False, force=False) is True
    assert should_process_doc(doc, only_missing=False, fix_suspicious=True, force=False) is True
    assert should_process_doc(
        {"publish_date": "2026-06-17T00:00:00", "scraped_at": "2026-07-02"},
        only_missing=True,
        fix_suspicious=False,
        force=False,
    ) is False


async def _run_resolve(doc, index=None):
    return await resolve_publish_date(
        doc,
        id_publish_index=index or {},
        fetch_get_info_api=False,
        get_info_cache={},
    )


def test_resolve_publish_date_prefers_alllist_index():
    doc = {
        "url": "https://bid.powerchina.cn/notice/detail?id=2409467417",
        "content_text": "",
    }
    index = {"2409467417": datetime(2026, 6, 17, 10, 25, 1)}
    import asyncio

    dt, reason = asyncio.run(_run_resolve(doc, index))
    assert reason == "allList_index"
    assert dt.day == 17


def test_apply_publish_date_update_dry_run():
    mongo_repo = MagicMock()
    doc = {"title": "测试", "publish_date": None, "content_hash": "abc"}
    changed = apply_publish_date_update(
        mongo_repo,
        doc,
        datetime(2026, 6, 17),
        dry_run=True,
    )
    assert changed is True
    mongo_repo._notices.update_one.assert_not_called()
