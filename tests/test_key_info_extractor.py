"""关键信息抽取（金额等）单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.key_info_extractor import (
    extract_key_info,
    looks_valid_stored_amount,
)
from src.core.notice_field_utils import parse_amount_yuan, structured_fields_from_doc

_JIANGXI_CONTENT = (
    "招标概要\n AI导读： 项目编号：JXTC2026070197 "
    "项目名称：江西工业职业技术学院BIM实训机房建设项目 "
    "采购方式：竞争性谈判 预算金额及最高限价：944,500.00元 采购内容包括： "
    "- 台式计算机 100台，采购预算780,000.00元； "
    "- 交换设备 3台，采购预算12,000.00元；"
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("944,500.00元", 944_500.0),
        ("944500.00 元", 944_500.0),
        ("780,000.00元", 780_000.0),
    ],
)
def test_parse_amount_yuan_with_thousand_separator(text, expected):
    assert parse_amount_yuan(text) == expected


@pytest.mark.parametrize(
    "value,valid",
    [
        ("944", False),
        ("及最高限价：944", False),
        ("944,500.00元", True),
        ("944500.00 元", True),
        ("650（万元）", True),
        ("1021000元", True),
    ],
)
def test_looks_valid_stored_amount(value, valid):
    assert looks_valid_stored_amount(value) is valid


def test_extract_key_info_jiangxi_bim_budget():
    info = extract_key_info(
        _JIANGXI_CONTENT,
        "江西工业职业技术学院BIM实训机房建设项目性谈判公告",
        use_llm=False,
    )
    assert info["budget_amount"] == "944,500.00元"
    assert parse_amount_yuan(info["budget_amount"]) == 944_500.0


def test_structured_fields_refreshes_bad_stored_amount():
    doc = {
        "title": "江西工业职业技术学院BIM实训机房建设项目性谈判公告",
        "content_text": _JIANGXI_CONTENT,
        "budget_amount": "944",
        "contract_amount": "944",
        "budget": "及最高限价：944",
    }
    fields = structured_fields_from_doc(doc, use_llm=False)
    assert fields["budget_amount"] == "944,500.00元"
    assert fields["amount_yuan"] == 944_500.0


def test_extract_key_info_llm_primary_with_rules_fallback():
    llm_payload = {
        "contract_amount": "950,000.00元",
        "tender_party": "江西工业职业技术学院",
        "project_period": "60日历天",
        "key_summary": "BIM实训机房建设及设备采购",
    }
    with patch("src.core.key_info_extractor._llm_extract", return_value=llm_payload):
        with patch("src.core.key_info_extractor._llm_configured", return_value=True):
            info = extract_key_info(_JIANGXI_CONTENT, "江西BIM项目", use_llm=True)
    assert info["budget_amount"] == "944,500.00元"
    assert info["contract_amount"] == "950,000.00元"
    assert info["tender_party"] == "江西工业职业技术学院"
    assert info["project_period"] == "60日历天"
    assert "BIM实训机房" in (info["key_summary"] or "")


def test_structured_fields_from_doc_llm_then_normalize():
    doc = {
        "title": "江西工业职业技术学院BIM实训机房建设项目",
        "content_text": _JIANGXI_CONTENT,
    }
    llm_payload = {
        "contract_amount": "944,500.00元",
        "tender_party": "江西工业职业技术学院",
        "project_period": "交货期/工期：60日历天 注：详见文件",
        "key_summary": "和范围：BIM实训机房台式机与交换设备采购 二、申请人资格能力要求",
    }
    with patch("src.core.key_info_extractor._llm_extract", return_value=llm_payload):
        with patch("src.core.key_info_extractor._llm_configured", return_value=True):
            fields = structured_fields_from_doc(doc, use_llm=True)
    assert fields["extraction_source"] == "llm+rules"
    assert fields["amount_yuan"] == 944_500.0
    assert fields["project_period"] == "60日历天"
    assert fields["key_summary"] == "BIM实训机房台式机与交换设备采购"
