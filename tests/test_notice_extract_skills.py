"""公告结构化字段抽取 skill 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_daily_brief_service import _build_top_item_summary
from src.core.notice_field_utils import extract_notice_fields_from_doc
from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS
from src.web.notice_extract_skills import extract_notice_fields

_HUARUN_KEY_SUMMARY = (
    "和范围：深圳华润中心大厦改造室内施工图及BIM设计顾问招标 "
    "交货期/工期：至2028年12月 注：详细内容见资格预审文件，以资格预审文件为准。 "
    "二、申请人资格能力要求 1、业绩要求：2021年1月至今，写字楼项目室内设计经验不少于2个；"
)

_POWERCHINA_PDF_TEXT = (
    "中国电建集团华东勘测设计研究院有限公司 BIM正向设计平台采购项目招标公告\n"
    "招标人：中国电建集团华东勘测设计研究院有限公司\n"
    "合同估算价：866万元\n"
    "工期：180日历天\n"
    "项目概况：本项目拟采购BIM正向设计平台及协同交付服务，涵盖模型创建、碰撞检测与成果交付。\n"
    "投标人资格：须具备工程设计综合甲级资质。"
)


def test_default_tool_schemas_include_extract_notice_fields():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    assert "skill_extract_notice_fields" in names


def test_extract_notice_fields_huarun_messy_key_summary():
    doc = {
        "title": "深圳华润中心大厦改造室内施工图及BIM设计顾问资审公告",
        "content_text": "正文",
        "key_summary": _HUARUN_KEY_SUMMARY,
        "tender_party": "华润(深圳)有限公司",
    }
    fields = extract_notice_fields_from_doc(doc, use_llm=False)
    assert fields["tender_party"] == "华润(深圳)有限公司"
    assert fields["project_period"] == "至2028年12月"
    assert fields["key_content"] == "深圳华润中心大厦改造室内施工图及BIM设计顾问招标"


def test_extract_notice_fields_powerchina_pdf_content():
    doc = {
        "title": "中国电建华东院BIM正向设计平台采购项目招标公告",
        "content_text": _POWERCHINA_PDF_TEXT,
    }
    fields = extract_notice_fields_from_doc(doc, use_llm=False)
    assert fields["tender_party"] == "中国电建集团华东勘测设计研究院有限公司"
    assert fields["contract_amount"] == "866万元"
    assert fields["amount_yuan"] == 8_660_000
    assert fields["project_period"] == "180日历天"
    assert "BIM正向设计平台" in (fields["key_content"] or "")


def test_extract_notice_fields_skill_from_content_text():
    result = extract_notice_fields(
        title="电建BIM项目",
        content_text=_POWERCHINA_PDF_TEXT,
        use_llm=False,
    )
    assert result["ok"] is True
    assert result["contract_amount"] == "866万元"
    assert result["tender_party"] == "中国电建集团华东勘测设计研究院有限公司"


def test_build_top_item_summary_uses_extract_pipeline():
    doc = {
        "title": "深圳华润中心大厦改造室内施工图及BIM设计顾问资审公告",
        "key_summary": _HUARUN_KEY_SUMMARY,
        "tender_party": "华润(深圳)有限公司",
    }
    summary = _build_top_item_summary(doc, use_llm=False)
    assert "招标方：华润(深圳)有限公司" in summary
    assert "项目周期：至2028年12月" in summary
    assert "招标关键内容：深圳华润中心大厦改造室内施工图及BIM设计顾问招标" in summary


def test_extract_notice_fields_defaults_llm_when_content_available():
    doc = {
        "title": "电建BIM项目",
        "content_text": _POWERCHINA_PDF_TEXT,
    }
    llm_payload = {
        "contract_amount": "866万元",
        "tender_party": "中国电建集团华东勘测设计研究院有限公司",
        "project_period": "180日历天",
        "key_summary": "采购BIM正向设计平台及协同交付服务",
    }
    with patch("src.core.key_info_extractor._llm_extract", return_value=llm_payload):
        with patch("src.core.key_info_extractor._llm_configured", return_value=True):
            fields = extract_notice_fields_from_doc(doc)
    assert fields["contract_amount"] == "866万元"
    assert fields["extraction_source"].startswith("llm")


def test_skill_extract_notice_fields_executor():
    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "skill_extract_notice_fields",
        {
            "title": "电建BIM项目",
            "content_text": _POWERCHINA_PDF_TEXT,
            "use_llm": False,
        },
    )
    assert result["ok"] is True
    assert result["contract_amount"] == "866万元"
    assert "866万元" in summary or "招标方" in summary


def test_skill_extract_notice_fields_persist():
    mock_repo = MagicMock()
    mock_repo.available = True
    mock_repo._notices = MagicMock()
    mock_repo._notices.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
    mock_repo._bim_notices = None

    doc = {
        "title": "电建BIM项目",
        "content_text": _POWERCHINA_PDF_TEXT,
        "content_hash": "abc123",
        "url": "https://bid.powerchina.cn/notice/detail?id=2409467417",
    }
    with patch("src.web.notice_extract_skills._get_mongo_repo", return_value=mock_repo):
        result = extract_notice_fields(doc=doc, persist=True, use_llm=False)

    assert result["ok"] is True
    assert result["persisted"] is True
    mock_repo._notices.update_one.assert_called_once()
    update_doc = mock_repo._notices.update_one.call_args[0][1]["$set"]
    assert update_doc["contract_amount"] == "866万元"
    assert update_doc["tender_party"] == "中国电建集团华东勘测设计研究院有限公司"
    assert update_doc["project_period"] == "180日历天"
    assert "BIM正向设计平台" in update_doc["key_summary"]
