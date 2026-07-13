"""公告通用标签工具。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.models import BidNotice
from src.core.notice_tags import (
    WECHAT_TAG,
    apply_notice_tags,
    effective_notice_tags,
    is_wechat_doc,
    is_wechat_notice,
)


def test_is_wechat_doc_by_site_id():
    assert is_wechat_doc({"source_site_id": "wechat_lpfb"}) is True
    assert is_wechat_doc({"source_site_id": "crec_bidding"}) is False


def test_effective_notice_tags_legacy_wechat():
    assert effective_notice_tags({"source_site_id": "wechat_lpfb"}) == [WECHAT_TAG]


def test_effective_notice_tags_stored():
    doc = {"tags": ["行业资讯", WECHAT_TAG], "source_site_id": "wechat_lpfb"}
    assert WECHAT_TAG in effective_notice_tags(doc)
    assert "行业资讯" in effective_notice_tags(doc)


def test_apply_notice_tags_for_wechat_site():
    notice = BidNotice(
        title="测试",
        url="https://mp.weixin.qq.com/s/x",
        source_site_id="wechat_lpfb",
        source_site_name="临平发布",
        source_url="https://example.com",
    )
    apply_notice_tags(notice, site={"id": "wechat_lpfb", "category": "wechat"})
    assert notice.tags == [WECHAT_TAG]


def test_is_wechat_notice_without_site():
    notice = BidNotice(
        title="测试",
        url="https://example.com",
        source_site_id="wechat_xzy",
        source_site_name="新智元",
        source_url="https://example.com",
    )
    assert is_wechat_notice(notice) is True


def test_normalize_user_tags_dedupes():
    from src.core.notice_tags import normalize_user_tags, persistable_notice_tags

    assert normalize_user_tags([" 重要 ", "重要", "", "行业资讯"]) == ["重要", "行业资讯"]


def test_persistable_notice_tags_keeps_wechat():
    from src.core.notice_tags import persistable_notice_tags

    doc = {"source_site_id": "wechat_lpfb", "tags": []}
    tags = persistable_notice_tags(doc, ["行业资讯"])
    assert WECHAT_TAG in tags
    assert "行业资讯" in tags
