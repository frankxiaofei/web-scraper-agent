#!/usr/bin/env python3
"""对 ccgp 等站点公告样本测试 BIM 分类（需 .env 中 LLM 配置）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_classifier import classify_bim_notice, is_bim_classify_enabled
from src.core.config import get_settings
from src.db.mongo_repository import create_mongo_repository

SAMPLE_SOURCES = ("ccgp_national", "ccgp", "ccgp_北京市", "ccgp_上海市")


def _load_samples(limit: int = 3) -> list[dict]:
    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if mongo_repo and mongo_repo.available and mongo_repo._notices is not None:
        col = mongo_repo._notices
        query = {
            "$and": [
                {
                    "$or": [
                        {"source_site_id": {"$in": list(SAMPLE_SOURCES)}},
                        {"source": {"$in": list(SAMPLE_SOURCES)}},
                        {"site_id": {"$in": list(SAMPLE_SOURCES)}},
                    ]
                },
                {"content_text": {"$exists": True, "$nin": [None, ""]}},
            ]
        }
        docs = list(col.find(query).sort("crawled_at", -1).limit(limit))
        mongo_repo.close()
        if docs:
            return docs

    return [
        {
            "title": "某市智慧工地 BIM 平台建设项目公开招标公告",
            "content_text": (
                "采购内容：建设基于 BIM 的智慧工地管理平台，含 Revit 模型集成、"
                "施工进度 4D 模拟及数字孪生可视化模块。"
            ),
            "key_summary": "智慧工地 BIM 平台，含 Revit 与数字孪生",
        },
        {
            "title": "办公桌椅采购项目竞争性谈判公告",
            "content_text": "采购内容：办公桌 50 套、办公椅 50 把，要求环保材料，送货上门安装。",
            "key_summary": "办公家具采购",
        },
        {
            "title": "轨道交通车站 BIM 咨询与建模服务采购公告",
            "content_text": (
                "服务范围：车站全专业 BIM 建模、碰撞检测、竣工模型交付及 CIM 平台数据对接。"
            ),
            "key_summary": "轨道交通 BIM 咨询与建模",
        },
    ][:limit]


def main() -> int:
    if not is_bim_classify_enabled():
        print("请设置 BIM_CLASSIFY_LLM=true 与 OPENAI_API_KEY（及 LLM_BASE_URL / LLM_MODEL）")
        return 1

    settings = get_settings()
    print(f"模型: {settings.llm_model}")
    print(f"Base URL: {settings.llm_base_url or '(OpenAI 默认)'}")
    print("-" * 60)

    samples = _load_samples(limit=3)
    if not samples:
        print("未找到样本数据")
        return 1

    for i, doc in enumerate(samples, 1):
        title = doc.get("title") or ""
        content = doc.get("content_text") or ""
        summary = doc.get("key_summary")
        print(f"\n[{i}] {title[:60]}")
        result = classify_bim_notice(title, content, summary, rate_limit=i > 1)
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))

    print("\nBIM 分类测试完成 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
