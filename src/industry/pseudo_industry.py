"""伪行业码解析与文档过滤（Phase 0）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.agri_classifier import (
    infer_agri_application_scenes,
    infer_agri_tech_types,
    matched_agri_keywords,
)
from src.web.biz_clue_service import match_product_lines

SUPPORTED_TYPES = frozenset({"product_line", "tag", "domain", "gb2017"})


@dataclass(frozen=True)
class PseudoIndustryCode:
    type: str
    name: str
    raw: str


def parse_industry_code(raw: str) -> PseudoIndustryCode:
    text = (raw or "").strip()
    if not text:
        raise ValueError("industry_code 不能为空")
    if ":" in text:
        prefix, name = text.split(":", 1)
        prefix = prefix.strip()
        name = name.strip()
        if prefix not in SUPPORTED_TYPES:
            raise ValueError(f"unsupported pseudo code type: {prefix}")
        if not name and prefix != "domain":
            raise ValueError(f"invalid industry_code: {raw}")
        return PseudoIndustryCode(type=prefix, name=name, raw=text)
    return PseudoIndustryCode(type="gb2017", name=text, raw=text)


def _doc_tags(doc: dict[str, Any]) -> set[str]:
    tags: set[str] = set()
    for raw in doc.get("agri_tags") or []:
        tags.add(str(raw))
    tags.update(infer_agri_tech_types(doc))
    tags.update(infer_agri_application_scenes(doc))
    tags.update(matched_agri_keywords(doc))
    return tags


def filter_docs_by_code(docs: list[dict[str, Any]], code: PseudoIndustryCode) -> list[dict[str, Any]]:
    if code.type == "domain":
        return docs
    if code.type == "product_line":
        return [
            d
            for d in docs
            if any(m.get("product_line") == code.name for m in match_product_lines(d))
        ]
    if code.type == "tag":
        return [d for d in docs if code.name in _doc_tags(d)]
    if code.type == "gb2017":
        return docs
    raise ValueError(f"unsupported pseudo code: {code.raw}")


def industry_display_name(code: PseudoIndustryCode) -> str:
    if code.type == "product_line":
        return f"产品线·{code.name}"
    if code.type == "tag":
        return f"标签·{code.name}"
    if code.type == "domain":
        return code.name or "全行业"
    return code.name
