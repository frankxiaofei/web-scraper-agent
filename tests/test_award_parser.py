"""award_parser 单元测试（P2-T-004）。"""

from __future__ import annotations

from src.core.models import BidNotice, NoticeCategory
from src.industry.enrichment.award_parser import parse_award_relations


def test_parse_winner_from_content_label():
    notice = BidNotice(
        title="某项目中标公告",
        url="https://example.com/1",
        source_site_id="test",
        source_site_name="test",
        source_url="https://example.com",
        category=NoticeCategory.WIN,
        content_text="采购人：某市农业农村局\n中标供应商：智慧农业科技有限公司",
    )
    result = parse_award_relations(notice)
    assert result.winner_name == "智慧农业科技有限公司"
    assert result.issuer_name == "某市农业农村局"
    assert result.confidence >= 0.9


def test_parse_winner_from_tender_party():
    notice = BidNotice(
        title="成交结果公告",
        url="https://example.com/2",
        source_site_id="test",
        source_site_name="test",
        source_url="https://example.com",
        category=NoticeCategory.WIN,
        tender_party="数字田园股份有限公司",
        purchaser="某省农业厅",
    )
    result = parse_award_relations(notice)
    assert result.winner_name == "数字田园股份有限公司"
    assert result.confidence >= 0.8


def test_skips_non_win_notice():
    notice = BidNotice(
        title="招标公告",
        url="https://example.com/3",
        source_site_id="test",
        source_site_name="test",
        source_url="https://example.com",
        category=NoticeCategory.TENDER,
    )
    result = parse_award_relations(notice)
    assert result.winner_name is None
    assert result.method == "skipped_not_win"
