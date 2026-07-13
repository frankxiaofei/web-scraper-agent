"""公告结构化字段解析单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.notice_field_utils import (
    aggregate_structured_insights,
    normalize_key_summary,
    normalize_project_period,
    parse_amount_yuan,
    structured_fields_from_doc,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("650（万元）", 6_500_000),
        ("1021000元", 1_021_000),
        ("866万元人民币", 8_660_000),
        ("4272.1 万元", 42_721_000),
        ("1.2亿元", 120_000_000),
        ("—", None),
        ("", None),
    ],
)
def test_parse_amount_yuan(text, expected):
    assert parse_amount_yuan(text) == expected


def test_structured_fields_from_doc_with_stored_fields():
    doc = {
        "title": "BIM 平台采购",
        "content_text": "正文",
        "budget_amount": "500万元",
        "tender_party": "中国电建某局",
        "bid_deadline": "2026-07-01 09:00",
        "project_location": "北京市朝阳区",
        "qualification_requirements": "具备工程设计甲级资质",
    }
    fields = structured_fields_from_doc(doc)
    assert fields["amount_yuan"] == 5_000_000
    assert fields["tender_party"] == "中国电建某局"
    assert fields["project_location"] == "北京市朝阳区"
    assert "甲级" in (fields["qualification_requirements"] or "")


def test_structured_fields_extract_from_content():
    doc = {
        "title": "测试项目",
        "content_text": (
            "采购人：某某建设集团有限公司\n"
            "预算金额：120万元\n"
            "建设地点：上海市浦东新区\n"
            "资质要求：投标人须具备建筑行业甲级设计资质\n"
        ),
    }
    fields = structured_fields_from_doc(doc)
    assert fields["amount_yuan"] == 1_200_000
    assert "建设集团" in (fields["tender_party"] or "")
    assert "浦东" in (fields["project_location"] or "")
    assert fields["qualification_requirements"]


def test_aggregate_structured_insights():
    docs = [
        {
            "title": "A",
            "content_text": "采购人：甲建设集团有限公司\n预算金额：50万元\n资质要求：BIM 咨询业绩",
            "is_bim_related": True,
        },
        {
            "title": "B",
            "content_text": "采购人：甲建设集团有限公司\n预算金额：800万元",
            "is_bim_related": True,
        },
        {
            "title": "C",
            "content_text": "普通采购无金额",
            "is_bim_related": True,
        },
    ]
    result = aggregate_structured_insights(docs)
    assert result["total"] == 3
    assert result["with_price"] == 2
    assert result["top_tender_parties"][0]["name"] == "甲建设集团有限公司"
    assert result["top_tender_parties"][0]["count"] == 2
    kw_names = [k["keyword"] for k in result["requirement_keywords"]]
    assert "BIM" in kw_names


_HUARUN_KEY_SUMMARY = (
    "和范围：深圳华润中心大厦改造室内施工图及BIM设计顾问招标 "
    "交货期/工期：至2028年12月 注：详细内容见资格预审文件，以资格预审文件为准。 "
    "二、申请人资格能力要求 1、业绩要求：2021年1月至今，写字楼项目室内设计经验不少于2个；"
)


def test_normalize_key_summary_huarun_example():
    """华润资审公告：去掉标签碎片与资格段落，只保留招标范围描述。"""
    result = normalize_key_summary(_HUARUN_KEY_SUMMARY)
    assert result == "深圳华润中心大厦改造室内施工图及BIM设计顾问招标"


def test_normalize_project_period_extracts_from_key_summary():
    """项目周期从摘要中的交货期字段补提，且不与关键内容重复。"""
    period = normalize_project_period(None, raw_key_summary=_HUARUN_KEY_SUMMARY)
    assert period == "至2028年12月"


def test_normalize_key_summary_strips_includes_prefix():
    raw = "包括：台式计算机100台，型号及技术要求详见公告附件；业绩要求：近3年"
    assert normalize_key_summary(raw) == "台式计算机100台，型号及技术要求详见公告附件"


def test_structured_fields_normalizes_messy_key_summary():
    doc = {
        "title": "深圳华润中心大厦改造室内施工图及BIM设计顾问资审公告",
        "content_text": "正文",
        "key_summary": _HUARUN_KEY_SUMMARY,
        "tender_party": "华润(深圳)有限公司",
    }
    fields = structured_fields_from_doc(doc)
    assert fields["key_summary"] == "深圳华润中心大厦改造室内施工图及BIM设计顾问招标"
    assert fields["project_period"] == "至2028年12月"
