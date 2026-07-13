"""工程大模型应用报告筛选与推送测试。"""

from __future__ import annotations

from src.core.engineering_llm_report_service import (
    build_engineering_llm_feishu_card,
    is_engineering_llm_candidate,
)
from src.core.feishu_push import send_feishu_interactive_card_to_users


def test_is_engineering_llm_candidate():
    assert is_engineering_llm_candidate({"title": "某工程大模型应用平台采购公告"}) is True
    assert is_engineering_llm_candidate({"title": "水利工程施工总承包", "key_summary": "无 AI"}) is False
    assert is_engineering_llm_candidate(
        {"title": "电力工程智能体平台建设项目招标", "key_summary": "大模型应用"}
    ) is True


def test_build_engineering_llm_feishu_card():
    brief = {
        "date": "2026-07-10",
        "days": 1,
        "count": 2,
        "by_site": {"电建": 2},
        "top_items": [
            {"title": "测试标讯", "url": "https://example.com/a", "source_site_name": "电建"},
        ],
    }
    card = build_engineering_llm_feishu_card(brief)
    assert "工程大模型应用" in card["header"]["title"]["content"]
    content = card["elements"][0]["text"]["content"]
    assert "2 条" in content


def test_send_feishu_interactive_card_to_users_dry_run():
    result = send_feishu_interactive_card_to_users(
        {"elements": []},
        ["yuan_jp", "li_xf10"],
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert len(result["results"]) == 2
