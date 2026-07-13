"""公告结构化字段解析与洞察聚合辅助。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

from src.core.key_info_extractor import (
    _llm_configured,
    _resolve_use_llm,
    extract_key_info,
    looks_valid_stored_amount,
)

# 常见资质/招标要求关键词（用于频次统计）
_REQUIREMENT_KEYWORDS = (
    "BIM",
    "建筑信息模型",
    "数字孪生",
    "Revit",
    "Autodesk",
    "一级资质",
    "二级资质",
    "甲级",
    "乙级",
    "安全生产许可证",
    "ISO9001",
    "ISO14001",
    "业绩要求",
    "类似项目",
    "注册资本",
    "项目负责人",
    "建造师",
    "高级工程师",
    "电子与智能化",
    "工程设计",
    "工程咨询",
    "CMMI",
    "联合体",
    "不接受联合体",
    "信用中国",
    "失信被执行人",
)

_AMOUNT_UNIT_MULTIPLIERS = {
    "元": 1.0,
    "万元": 1e4,
    "万": 1e4,
    "亿元": 1e8,
    "亿": 1e8,
}

_PRICE_BUCKETS: list[tuple[str, float, Optional[float]]] = [
    ("未披露", 0, 0),
    ("10万以下", 0, 1e5),
    ("10万-100万", 1e5, 1e6),
    ("100万-500万", 1e6, 5e6),
    ("500万-1000万", 5e6, 1e7),
    ("1000万-5000万", 1e7, 5e7),
    ("5000万-1亿", 5e7, 1e8),
    ("1亿以上", 1e8, None),
]

_KEY_SUMMARY_STOP_RE = re.compile(
    r"(?:"
    r"注[：:]\s*详细内容见"
    r"|交货期[/／]工期[：:]"
    r"|服务期限[：:]"
    r"|项目周期[：:]"
    r"|合同履行期限[：:]"
    r"|计划工期[：:]"
    r"|工期[：:]"
    r"|[一二三四五六七八九十百千]+、申请人"
    r"|资格(?:能力)?要求"
    r"|业绩要求"
    r"|投标人资格"
    r"|供应商资格"
    r"|采购预算[：:]"
    r"|预算金额[：:]"
    r"|招标人[：:]"
    r"|评标方法[：:]"
    r"|定标方法[：:]"
    r"|发包工程估算价[：:]"
    r"|投标人资质要求[：:]"
    r"|项目负责人资格[：:]"
    r"|参与本项目需满足"
    r"|采购范围[：:]"
    r")"
)

_KEY_SUMMARY_LEADING_LABEL_RE = re.compile(
    r"^(?:"
    r"和范围"
    r"|内容和范围"
    r"|招标范围"
    r"|项目概况"
    r"|采购内容"
    r"|采购需求"
    r"|项目简介"
    r"|项目说明"
    r"|包括"
    r")\s*[：:\s]*"
)

_PERIOD_EXTRACT_RE = re.compile(
    r"交货期[/／]工期[：:]\s*([^。；;\n]+)"
    r"|服务期限[：:]\s*([^。；;\n]+)"
    r"|合同履行期限[：:]\s*([^。；;\n]+)"
    r"|项目周期[：:]\s*([^。；;\n]+)"
    r"|计划工期[：:]\s*([^。；;\n]+)"
    r"|工期[：:]\s*([^。；;\n]+)"
)

_PROJECT_PERIOD_STOP_RE = re.compile(
    r"(?:开标时间|采购项目概况|注[：:]|资格要求|业绩要求|项目地址[：:]|项目规模[：:])"
)


def extract_period_from_text(text: str | None) -> str | None:
    """从摘要或正文中提取工期/服务期限（标签式字段）。"""
    if not text:
        return None
    m = _PERIOD_EXTRACT_RE.search(str(text))
    if not m:
        return None
    value = next((g for g in m.groups() if g), "").strip()
    return value or None


def normalize_key_summary(text: str | None, *, max_len: int = 100) -> str | None:
    """清洗招标关键内容：去标签碎片、截断资格/注等段落、限制长度。"""
    if not text or not str(text).strip():
        return None

    cleaned = re.sub(r"\s+", " ", str(text).strip())
    stop = _KEY_SUMMARY_STOP_RE.search(cleaned)
    if stop:
        cleaned = cleaned[: stop.start()].strip()

    for _ in range(3):
        updated = _KEY_SUMMARY_LEADING_LABEL_RE.sub("", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated

    cleaned = re.sub(r"^\*+[：:\s]*", "", cleaned).strip()
    cleaned = cleaned.rstrip("；;，,。.")
    if not cleaned:
        return None

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned


def normalize_project_period(
    period: str | None,
    *,
    raw_key_summary: str | None = None,
    max_len: int = 60,
) -> str | None:
    """规范化项目周期；缺失时从原始摘要补提，并去掉嵌入的其他字段。"""
    text = (period or "").strip()
    if not text and raw_key_summary:
        text = extract_period_from_text(raw_key_summary) or ""

    if not text:
        return None

    stop = _PROJECT_PERIOD_STOP_RE.search(text)
    if stop:
        text = text[: stop.start()].strip()

    text = re.sub(
        r"^(?:交货期[/／]工期|服务期限|合同履行期限|项目周期|计划工期|工期)\s*[：:]\s*",
        "",
        text,
    ).strip()

    if not text:
        return None
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def parse_amount_yuan(text: str | None) -> Optional[float]:
    """将预算/金额文本解析为人民币元（浮点）。"""
    if not text or str(text).strip() in ("—", "-", "None", "null"):
        return None
    raw = re.sub(r"\s+", "", str(text))
    if not raw or not re.search(r"\d", raw):
        return None

    m = re.search(
        r"([\d,，.]+)\s*(亿元|亿|万元|万|元)?",
        raw,
    )
    if not m:
        return None
    num_str = m.group(1).replace(",", "").replace("，", "")
    try:
        value = float(num_str)
    except ValueError:
        return None

    unit = m.group(2) or ""
    if not unit:
        if "亿" in raw:
            unit = "亿"
        elif "万" in raw:
            unit = "万"
        else:
            unit = "元"
    multiplier = _AMOUNT_UNIT_MULTIPLIERS.get(unit, 1.0)
    return value * multiplier


def _first_non_empty(doc: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = doc.get(key)
        if val is not None and str(val).strip() and str(val).strip() not in ("—", "-"):
            return str(val).strip()
    return ""


def _merge_extracted(
    stored: dict[str, str],
    extracted: dict[str, Optional[str]],
) -> None:
    mapping = {
        "budget_raw": "budget_amount",
        "contract_raw": "contract_amount",
        "tender_party": "tender_party",
        "agency": "agency",
        "bid_deadline": "bid_deadline",
        "open_time": "open_time",
        "project_period": "project_period",
        "project_location": "project_location",
        "qualification": "qualification_requirements",
        "key_summary": "key_summary",
    }
    for stored_key, extracted_key in mapping.items():
        if not stored.get(stored_key) and extracted.get(extracted_key):
            stored[stored_key] = str(extracted[extracted_key]).strip()


def _doc_has_extractable_content(doc: dict[str, Any]) -> bool:
    return bool((doc.get("content_text") or "").strip() or (doc.get("key_summary") or "").strip())


def _merge_llm_into_stored(
    stored: dict[str, str],
    llm_data: dict[str, Optional[str]],
) -> None:
    mapping = {
        "budget_raw": "budget_amount",
        "contract_raw": "contract_amount",
        "tender_party": "tender_party",
        "agency": "agency",
        "bid_deadline": "bid_deadline",
        "open_time": "open_time",
        "project_period": "project_period",
        "project_location": "project_location",
        "qualification": "qualification_requirements",
        "key_summary": "key_summary",
    }
    for stored_key, llm_key in mapping.items():
        val = llm_data.get(llm_key)
        if val and str(val).strip():
            stored[stored_key] = str(val).strip()


def extract_notice_fields_from_doc(
    doc: dict[str, Any],
    *,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    """抽取并规范化报告所需的四项结构化字段。"""
    if use_llm is None and _doc_has_extractable_content(doc):
        use_llm = True
    structured = structured_fields_from_doc(doc, use_llm=use_llm)
    sources: list[str] = []
    extraction_source = structured.get("extraction_source") or "rules"
    if extraction_source == "llm":
        sources.append("llm")
    elif extraction_source == "llm+rules":
        sources.append("llm")
        sources.append("rules")
    else:
        if _first_non_empty(doc, "contract_amount", "budget_amount", "budget"):
            sources.append("stored")
        if doc.get("content_text") and structured.get("tender_party"):
            if "stored" not in sources or not _first_non_empty(doc, "tender_party", "purchaser"):
                sources.append("content")
        if doc.get("key_summary") and structured.get("project_period"):
            if not _first_non_empty(doc, "project_period"):
                sources.append("key_summary")
        if not sources:
            sources.append("rules")

    return {
        "contract_amount": structured.get("contract_amount") or structured.get("budget_amount"),
        "budget_amount": structured.get("budget_amount"),
        "tender_party": structured.get("tender_party"),
        "project_period": structured.get("project_period"),
        "key_content": structured.get("key_summary"),
        "amount_yuan": structured.get("amount_yuan"),
        "amount_display": structured.get("amount_display"),
        "extraction_source": "+".join(dict.fromkeys(sources)),
    }


def structured_fields_from_doc(
    doc: dict[str, Any],
    *,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    """从 Mongo/JSONL 文档提取或补全结构化字段。"""
    title = doc.get("title") or ""
    content = doc.get("content_text") or ""

    budget_raw = _first_non_empty(doc, "budget_amount", "budget")
    contract_raw = _first_non_empty(doc, "contract_amount")
    if budget_raw and not looks_valid_stored_amount(budget_raw):
        budget_raw = ""
    if contract_raw and not looks_valid_stored_amount(contract_raw):
        contract_raw = ""
    tender_party = _first_non_empty(doc, "tender_party", "purchaser")
    agency = _first_non_empty(doc, "agency")
    bid_deadline = _first_non_empty(doc, "bid_deadline")
    open_time = _first_non_empty(doc, "open_time")
    project_period = _first_non_empty(doc, "project_period")
    project_location = _first_non_empty(doc, "project_location", "region")
    qualification = _first_non_empty(doc, "qualification_requirements")
    key_summary = _first_non_empty(doc, "key_summary")

    fill_keys = (
        "budget_raw",
        "contract_raw",
        "tender_party",
        "agency",
        "bid_deadline",
        "open_time",
        "project_period",
        "project_location",
        "qualification",
        "key_summary",
    )
    stored = {
        "budget_raw": budget_raw,
        "contract_raw": contract_raw,
        "tender_party": tender_party,
        "agency": agency,
        "bid_deadline": bid_deadline,
        "open_time": open_time,
        "project_period": project_period,
        "project_location": project_location,
        "qualification": qualification,
        "key_summary": key_summary,
    }

    extraction_source = "stored" if any(stored[k] for k in fill_keys) else "rules"

    if content and _resolve_use_llm(use_llm, has_content=True):
        from src.core.key_info_extractor import _llm_extract

        llm_data = _llm_extract(content, title, metadata=doc) if _llm_configured() else None
        rules_data = extract_key_info(content, title, use_llm=False)
        if llm_data:
            _merge_llm_into_stored(stored, llm_data)
            _merge_extracted(stored, rules_data)
            extraction_source = "llm+rules"
        else:
            _merge_extracted(stored, rules_data)
            extraction_source = "rules"

    elif content and any(not stored[k] for k in fill_keys):
        extracted = extract_key_info(content, title, use_llm=False)
        _merge_extracted(stored, extracted)
        extraction_source = "rules"

    raw_key_summary = stored["key_summary"]
    if raw_key_summary and (not stored["project_period"] or not stored["contract_raw"] or not stored["budget_raw"]):
        from_key = extract_key_info(raw_key_summary, title, use_llm=False)
        if not stored["project_period"]:
            stored["project_period"] = (from_key.get("project_period") or "") or ""
        if not stored["contract_raw"]:
            stored["contract_raw"] = (from_key.get("contract_amount") or "") or ""
        if not stored["budget_raw"]:
            stored["budget_raw"] = (from_key.get("budget_amount") or "") or ""
        if not stored["tender_party"]:
            stored["tender_party"] = (from_key.get("tender_party") or "") or ""

    budget_raw = stored["budget_raw"]
    contract_raw = stored["contract_raw"]
    tender_party = stored["tender_party"]
    agency = stored["agency"]
    bid_deadline = stored["bid_deadline"]
    open_time = stored["open_time"]
    project_period = stored["project_period"]
    project_location = stored["project_location"]
    qualification = stored["qualification"]
    key_summary = stored["key_summary"]

    raw_key_summary = key_summary
    if not project_period and raw_key_summary:
        project_period = extract_period_from_text(raw_key_summary) or project_period

    amount_raw = budget_raw or contract_raw
    amount_yuan = parse_amount_yuan(amount_raw)

    return {
        "budget_amount": budget_raw or None,
        "contract_amount": contract_raw or None,
        "amount_display": amount_raw or None,
        "amount_yuan": amount_yuan,
        "tender_party": tender_party or None,
        "agency": agency or None,
        "bid_deadline": bid_deadline or None,
        "open_time": open_time or None,
        "project_period": normalize_project_period(project_period),
        "project_location": project_location or None,
        "qualification_requirements": qualification or None,
        "key_summary": normalize_key_summary(key_summary),
        "extraction_source": extraction_source,
    }


def extract_requirement_keywords(text: str) -> list[str]:
    """从正文/摘要中匹配常见资质与招标要求关键词。"""
    if not text:
        return []
    found: list[str] = []
    upper_text = text.upper()
    for kw in _REQUIREMENT_KEYWORDS:
        if kw.upper() in upper_text or kw in text:
            found.append(kw)
    return found


def _price_bucket_label(amount_yuan: Optional[float]) -> str:
    if amount_yuan is None or amount_yuan <= 0:
        return "未披露"
    for label, low, high in _PRICE_BUCKETS[1:]:
        if high is None and amount_yuan >= low:
            return label
        if high is not None and low <= amount_yuan < high:
            return label
    return "1亿以上"


def aggregate_structured_insights(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合公告列表的价格分布、招标人、要求关键词等。"""
    total = len(docs)
    if total == 0:
        return {
            "total": 0,
            "with_price": 0,
            "with_tender_party": 0,
            "with_requirements": 0,
            "price_buckets": [],
            "top_tender_parties": [],
            "requirement_keywords": [],
            "top_locations": [],
            "deadline_samples": [],
            "amount_stats": {},
        }

    bucket_counter: Counter[str] = Counter()
    tender_counter: Counter[str] = Counter()
    keyword_counter: Counter[str] = Counter()
    location_counter: Counter[str] = Counter()
    amounts: list[float] = []
    deadline_samples: list[dict[str, str]] = []

    for doc in docs:
        fields = structured_fields_from_doc(doc, use_llm=False)
        bucket_counter[_price_bucket_label(fields.get("amount_yuan"))] += 1

        amt = fields.get("amount_yuan")
        if isinstance(amt, (int, float)) and amt > 0:
            amounts.append(float(amt))

        tp = fields.get("tender_party")
        if tp and len(tp) >= 4 and not tp.startswith("为"):
            tender_counter[tp[:80]] += 1

        loc = fields.get("project_location")
        if loc and len(loc) >= 2:
            location_counter[loc[:60]] += 1

        qual = fields.get("qualification_requirements") or ""
        summary = fields.get("key_summary") or ""
        content = doc.get("content_text") or ""
        search_text = f"{qual}\n{summary}\n{content[:3000]}"
        for kw in extract_requirement_keywords(search_text):
            keyword_counter[kw] += 1

        deadline = fields.get("bid_deadline")
        if deadline and len(deadline_samples) < 8:
            deadline_samples.append(
                {
                    "title": (doc.get("title") or "")[:60],
                    "bid_deadline": deadline,
                    "tender_party": (tp or "")[:40],
                }
            )

    bucket_order = [label for label, _, _ in _PRICE_BUCKETS]
    price_buckets = [
        {"bucket": label, "count": bucket_counter.get(label, 0)}
        for label in bucket_order
        if bucket_counter.get(label, 0) > 0
    ]

    amount_stats: dict[str, Any] = {}
    if amounts:
        amounts.sort()
        amount_stats = {
            "min_yuan": amounts[0],
            "max_yuan": amounts[-1],
            "median_yuan": amounts[len(amounts) // 2],
            "avg_yuan": round(sum(amounts) / len(amounts), 2),
            "count": len(amounts),
        }

    return {
        "total": total,
        "with_price": len(amounts),
        "with_tender_party": sum(tender_counter.values()),
        "with_requirements": sum(
            1 for d in docs if structured_fields_from_doc(d, use_llm=False).get("qualification_requirements")
        ),
        "price_buckets": price_buckets,
        "top_tender_parties": [
            {"name": name, "count": cnt} for name, cnt in tender_counter.most_common(10)
        ],
        "requirement_keywords": [
            {"keyword": kw, "count": cnt} for kw, cnt in keyword_counter.most_common(15)
        ],
        "top_locations": [
            {"location": loc, "count": cnt} for loc, cnt in location_counter.most_common(8)
        ],
        "deadline_samples": deadline_samples,
        "amount_stats": amount_stats,
    }
