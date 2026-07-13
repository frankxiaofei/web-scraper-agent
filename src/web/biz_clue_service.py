"""商机洞察 Agent — 基于已采集 notices 的规则引擎分析。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src.core.agri_classifier import doc_is_agri_related, matched_agri_keywords
from src.core.biz_clue_config import (
    compute_next_check_date,
    get_config_version,
    get_scoring_levels,
    load_biz_clue_keywords,
    load_biz_clue_prompt,
)
from src.core.notice_field_utils import parse_amount_yuan, structured_fields_from_doc
from src.core.timezone_utils import app_now
from src.core.url_utils import resolve_notice_detail_url
from src.web.agri_insights_service import AgriInsightsService, get_agri_insights_service
from src.web.data_service import (
    _format_dt,
    doc_publish_timestamp,
    notice_content_href,
    notice_id_from_doc,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
ANALYSIS_CACHE_PATH = ROOT / "data" / "biz_clue_analysis_cache.json"
_ANALYSIS_CACHE_TTL_SECONDS = 120.0

_service: Optional["BizClueService"] = None
_analysis_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _doc_text(doc: dict[str, Any]) -> str:
    return "\n".join(
        str(doc.get(k) or "")
        for k in ("title", "content_text", "key_summary", "category")
    )


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _extract_region(doc: dict[str, Any], structured: dict[str, Any]) -> dict[str, str]:
    region_raw = (
        structured.get("project_location")
        or doc.get("region")
        or doc.get("province")
        or ""
    )
    region_raw = str(region_raw).strip()
    province = city = county = ""
    if region_raw:
        m = re.match(
            r"([\u4e00-\u9fa5]{2,}(?:省|自治区|市))?"
            r"([\u4e00-\u9fa5]{2,}(?:市|州|盟|地区))?"
            r"([\u4e00-\u9fa5]{2,}(?:区|县|旗))?",
            region_raw,
        )
        if m:
            province, city, county = (g or "" for g in m.groups())
        else:
            province = region_raw[:20]
    return {"province": province, "city": city, "county": county, "raw": region_raw}


def _is_excluded(doc: dict[str, Any]) -> tuple[bool, str]:
    cfg = load_biz_clue_keywords()
    text = _doc_text(doc)
    for kw in cfg.get("exclude_keywords") or []:
        if kw in text:
            has_product = bool(match_product_lines(doc))
            if not has_product:
                return True, f"排除词: {kw}"
    return False, ""


def match_product_lines(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """按 bizclue 三类产品线匹配，返回 product_matches 列表。"""
    cfg = load_biz_clue_keywords()
    text = _doc_text(doc)
    matches: list[dict[str, Any]] = []
    for line_name, line_cfg in (cfg.get("product_lines") or {}).items():
        keywords = line_cfg.get("keywords") or []
        hit_kws = [kw for kw in keywords if kw in text]
        if not hit_kws:
            continue
        evidence = hit_kws[0]
        for kw in hit_kws:
            idx = text.find(kw)
            if idx >= 0:
                start = max(0, idx - 10)
                end = min(len(text), idx + len(kw) + 20)
                evidence = text[start:end].strip()
                break
        match_score = min(100, 40 + len(hit_kws) * 15)
        matches.append(
            {
                "product_line": line_name,
                "match_score": match_score,
                "evidence": evidence[:80],
                "keywords": hit_kws[:5],
            }
        )
    if not matches and doc_is_agri_related(doc):
        agri_kws = matched_agri_keywords(doc)
        if agri_kws:
            matches.append(
                {
                    "product_line": "农业平台",
                    "match_score": 50,
                    "evidence": agri_kws[0],
                    "keywords": agri_kws[:3],
                }
            )
    return matches


def identify_project_stage(doc: dict[str, Any]) -> dict[str, Any]:
    """识别项目阶段，返回 stage 名称、order、命中关键词。"""
    cfg = load_biz_clue_keywords()
    text = _doc_text(doc)
    best: dict[str, Any] | None = None
    for stage in cfg.get("stages") or []:
        name = stage.get("name") or ""
        order = int(stage.get("order") or 0)
        keywords = stage.get("keywords") or []
        hit = [kw for kw in keywords if kw in text]
        if not hit:
            continue
        candidate = {
            "stage": name,
            "order": order,
            "keywords": hit,
            "check_days": stage.get("check_days") or 7,
        }
        if best is None or order > best["order"]:
            best = candidate
    if best is None:
        category = str(doc.get("category") or "")
        if "招标" in category or "采购" in category:
            return {
                "stage": "正式采购",
                "order": 6,
                "keywords": [],
                "check_days": 1,
            }
        return {
            "stage": "其他",
            "order": 0,
            "keywords": [],
            "check_days": 14,
        }
    return best


def _infer_source_level(doc: dict[str, Any]) -> str:
    cfg = load_biz_clue_keywords()
    url = str(doc.get("url") or doc.get("detail_url") or doc.get("source_url") or "")
    domain = urlparse(url).netloc.lower()
    p0 = cfg.get("source_levels", {}).get("P0_domains") or []
    p1 = cfg.get("source_levels", {}).get("P1_domains") or []
    for d in p0:
        if d in domain:
            return "P0"
    for d in p1:
        if d in domain:
            return "P1"
    if _doc_source_id(doc):
        return "P2"
    return "P2"


def _score_funding(doc: dict[str, Any], structured: dict[str, Any]) -> tuple[int, list[str]]:
    cfg = load_biz_clue_keywords()
    text = _doc_text(doc)
    funding_cfg = cfg.get("funding_keywords") or {}
    if any(kw in text for kw in funding_cfg.get("已下达") or []):
        return 20, ["资金已下达或批复"]
    budget = structured.get("budget_amount") or structured.get("amount_display") or doc.get("budget_amount")
    if budget and parse_amount_yuan(str(budget)):
        return 15, ["已列预算或金额"]
    if any(kw in text for kw in funding_cfg.get("正在申报") or []):
        return 8, ["正在申报阶段"]
    return 0, ["无明确资金依据"]


def _score_authenticity(stage_info: dict[str, Any], source_level: str) -> tuple[int, list[str]]:
    stage = stage_info.get("stage") or ""
    if stage in ("采购前置", "正式采购") and source_level == "P0":
        return 15, ["官方采购意向/公告"]
    if stage in ("立项审批", "咨询设计", "项目申报"):
        return 10, ["官方名单或批复类信息"]
    if source_level == "P2":
        return 3, ["商业聚合来源，待核验"]
    return 8, ["权威来源线索"]


def _score_product_match(product_matches: list[dict[str, Any]]) -> tuple[int, list[str]]:
    lines = {m["product_line"] for m in product_matches}
    n = len(lines)
    if n >= 3:
        return 20, ["三类产品线均匹配"]
    if n == 2:
        return 15, ["两类产品线匹配"]
    if n == 1:
        return 8, ["单类产品线匹配"]
    return 0, ["未匹配目标产品线"]


def _score_early_value(stage_info: dict[str, Any]) -> tuple[int, list[str]]:
    stage = stage_info.get("stage") or ""
    if stage in ("咨询设计", "立项审批", "项目申报", "政策资金"):
        return 15, ["可研/需求形成期，提前介入价值高"]
    if stage == "采购前置":
        return 10, ["采购意向/需求调查阶段"]
    if stage == "正式采购":
        return 2, ["已进入正式招标"]
    return 5, ["阶段不明确"]


def _score_procurement_timing(doc: dict[str, Any]) -> tuple[int, list[str]]:
    text = _doc_text(doc)
    pub_ts = doc_publish_timestamp(doc)
    if not pub_ts:
        return 1, ["发布日期未知"]
    if any(kw in text for kw in ("30日内", "一个月内", "近期采购", "即将招标")):
        return 10, ["预计3个月内采购"]
    if any(kw in text for kw in ("三个月", "半年内", "本年度")):
        return 8, ["预计3-6个月采购窗口"]
    if any(kw in text for kw in ("明年", "下一年", "2027", "2028")):
        return 5, ["预计6-12个月窗口"]
    days_ago = (datetime.now() - pub_ts).days
    if days_ago <= 30:
        return 8, ["近期发布，关注度高"]
    return 3, ["采购时间不明确"]


def _score_entity_clarity(structured: dict[str, Any]) -> tuple[int, list[str]]:
    owner = structured.get("tender_party") or structured.get("project_owner")
    if owner and len(str(owner).strip()) >= 4:
        return 5, ["建设/采购主体清晰"]
    return 2, ["主体信息不完整"]


def _score_region(region: dict[str, str]) -> tuple[int, list[str]]:
    if region.get("province") and region.get("city"):
        return 5, ["省市区信息完整"]
    if region.get("province") or region.get("raw"):
        return 3, ["有地区信息"]
    return 1, ["地区不明确"]


def _score_completeness(
    doc: dict[str, Any],
    structured: dict[str, Any],
    product_matches: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    fields = [
        doc.get("title"),
        doc.get("url") or doc.get("detail_url"),
        structured.get("budget_amount") or structured.get("amount_display"),
        structured.get("tender_party"),
        structured.get("project_location") or doc.get("region"),
        product_matches,
        doc_publish_timestamp(doc),
    ]
    filled = sum(1 for f in fields if f)
    ratio = filled / len(fields)
    if ratio >= 0.9:
        return 5, ["关键字段完整度≥90%"]
    if ratio >= 0.7:
        return 3, ["关键字段完整度≥70%"]
    return 1, ["关键字段缺失较多"]


def _score_competition(stage_info: dict[str, Any]) -> tuple[int, list[str]]:
    stage = stage_info.get("stage") or ""
    if stage in ("政策资金", "项目申报", "立项审批", "咨询设计"):
        return 5, ["尚处开放形成期"]
    if stage == "采购前置":
        return 3, ["竞争态势待观察"]
    return 0, ["可能已进入锁定阶段"]


def compute_opportunity_score(
    doc: dict[str, Any],
    *,
    product_matches: Optional[list[dict[str, Any]]] = None,
    stage_info: Optional[dict[str, Any]] = None,
    source_level: Optional[str] = None,
    verification_status: str = "verified",
) -> dict[str, Any]:
    """按 bizclue 第7节评分表计算 100 分制商机评分。"""
    structured = structured_fields_from_doc(doc, use_llm=False)
    product_matches = product_matches if product_matches is not None else match_product_lines(doc)
    stage_info = stage_info if stage_info is not None else identify_project_stage(doc)
    source_level = source_level or _infer_source_level(doc)

    dimensions: list[tuple[str, int, list[str]]] = [
        ("资金确定性", *_score_funding(doc, structured)),
        ("项目真实性", *_score_authenticity(stage_info, source_level)),
        ("产品匹配度", *_score_product_match(product_matches)),
        ("提前介入价值", *_score_early_value(stage_info)),
        ("采购时间", *_score_procurement_timing(doc)),
        ("主体清晰度", *_score_entity_clarity(structured)),
        ("区域与交付", *_score_region(_extract_region(doc, structured))),
        ("信息完整度", *_score_completeness(doc, structured, product_matches)),
        ("竞争态势", *_score_competition(stage_info)),
    ]

    total = sum(d[1] for d in dimensions)
    score_reasons = [f"{name}+{pts}分: {reasons[0]}" for name, pts, reasons in dimensions if pts > 0]

    thresholds = get_scoring_levels()
    level = "C"
    if total >= thresholds["S"]:
        level = "S"
    elif total >= thresholds["A"]:
        level = "A"
    elif total >= thresholds["B"]:
        level = "B"

    if verification_status == "pending" and level in ("S", "A"):
        level = "B"
        score_reasons.append("待核验线索最高B级")

    risk_flags: list[str] = []
    if source_level == "P2":
        risk_flags.append("商业聚合来源")
    if not product_matches:
        risk_flags.append("产品线未匹配")

    return {
        "opportunity_score": min(100, total),
        "opportunity_level": level,
        "score_reasons": score_reasons,
        "risk_flags": risk_flags,
        "dimensions": {name: pts for name, pts, _ in dimensions},
    }


def _recommended_action(stage: str, level: str) -> str:
    if level in ("S", "A"):
        if stage in ("采购前置", "正式采购"):
            return "立即联系采购人，准备方案与资质材料"
        return "安排客户经理跟进，补充资金与主体信息"
    if stage in ("咨询设计", "立项审批"):
        return "关注后续采购意向，准备技术方案预沟通"
    if stage == "项目申报":
        return "跟踪申报结果，建立项目台账"
    return "纳入观察列表，定期复查"


def _missing_fields(doc: dict[str, Any], structured: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not (doc.get("url") or doc.get("detail_url")):
        missing.append("source_url")
    if not doc_publish_timestamp(doc):
        missing.append("publish_date")
    if not (structured.get("project_location") or doc.get("region")):
        missing.append("region")
    if not structured.get("tender_party"):
        missing.append("project_owner")
    if not (structured.get("budget_amount") or structured.get("amount_display")):
        missing.append("budget_amount")
    return missing


def _build_lead_id(doc: dict[str, Any], region: dict[str, str]) -> str:
    notice_id = notice_id_from_doc(doc) or "unknown"
    short = hashlib.md5(notice_id.encode()).hexdigest()[:8].upper()
    prov = (region.get("province") or "CN")[:2]
    date_str = app_now().strftime("%Y%m%d")
    return f"AGRI-{prov}-{short}-{date_str}"


def _lead_from_doc(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    excluded, exclude_reason = _is_excluded(doc)
    product_matches = match_product_lines(doc)
    if excluded or not product_matches:
        return None

    structured = structured_fields_from_doc(doc, use_llm=False)
    stage_info = identify_project_stage(doc)
    source_level = _infer_source_level(doc)
    verification_status = "verified" if source_level in ("P0", "P1") else "pending"
    score_result = compute_opportunity_score(
        doc,
        product_matches=product_matches,
        stage_info=stage_info,
        source_level=source_level,
        verification_status=verification_status,
    )
    region = _extract_region(doc, structured)
    pub_ts = doc_publish_timestamp(doc)
    budget_yuan = parse_amount_yuan(
        str(structured.get("budget_amount") or structured.get("amount_display") or doc.get("budget_amount") or "")
    )
    missing = _missing_fields(doc, structured)
    next_check = compute_next_check_date(stage_info.get("stage") or "")

    evidence_quotes: list[str] = []
    for pm in product_matches[:2]:
        if pm.get("evidence"):
            evidence_quotes.append(pm["evidence"])
    if stage_info.get("keywords"):
        evidence_quotes.append(f"阶段词: {stage_info['keywords'][0]}")

    url = resolve_notice_detail_url(
        doc.get("url") or doc.get("detail_url") or "",
        base_url=doc.get("source_url") or "",
    )
    notice_id = notice_id_from_doc(doc)
    dedup_key = _normalize_url(url) or notice_id

    return {
        "lead_id": _build_lead_id(doc, region),
        "notice_id": notice_id,
        "content_href": notice_content_href(notice_id),
        "project_name": (doc.get("title") or "").strip(),
        "province": region.get("province") or "",
        "city": region.get("city") or "",
        "county": region.get("county") or "",
        "region_display": region.get("raw") or region.get("province") or "—",
        "publisher": doc.get("source_site_name") or _doc_source_id(doc),
        "project_owner": structured.get("tender_party") or "",
        "end_user": "",
        "project_stage": stage_info.get("stage") or "其他",
        "publish_date": _format_dt(pub_ts) if pub_ts else "",
        "publish_date_iso": pub_ts.isoformat() if pub_ts else "",
        "planned_procurement_date": "",
        "budget_amount_yuan": budget_yuan,
        "budget_display": structured.get("budget_amount") or structured.get("amount_display") or "",
        "funding_source": "",
        "product_matches": product_matches,
        "construction_scope": [pm["product_line"] for pm in product_matches],
        "source_url": url,
        "source_level": source_level,
        "source_site_id": _doc_source_id(doc),
        "source_site_name": doc.get("source_site_name") or _doc_source_id(doc),
        "evidence_quotes": evidence_quotes[:3],
        "attachments": [],
        "verification_status": verification_status,
        "missing_fields": missing,
        "opportunity_score": score_result["opportunity_score"],
        "opportunity_level": score_result["opportunity_level"],
        "score_reasons": score_result["score_reasons"],
        "risk_flags": score_result["risk_flags"],
        "recommended_action": _recommended_action(
            stage_info.get("stage") or "", score_result["opportunity_level"]
        ),
        "next_check_date": next_check,
        "dedup_key": dedup_key,
        "exclude_reason": exclude_reason if excluded else "",
    }


def deduplicate_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL / dedup_key 去重，保留评分更高者。"""
    seen: dict[str, dict[str, Any]] = {}
    for lead in leads:
        key = lead.get("dedup_key") or lead.get("lead_id") or ""
        if not key:
            continue
        existing = seen.get(key)
        if existing is None or lead.get("opportunity_score", 0) > existing.get("opportunity_score", 0):
            seen[key] = lead
    result = list(seen.values())
    result.sort(key=lambda x: x.get("opportunity_score") or 0, reverse=True)
    return result


class BizClueService:
    """商机洞察 Agent 服务。"""

    def __init__(self, agri_service: Optional[AgriInsightsService] = None) -> None:
        self._agri = agri_service or get_agri_insights_service()

    def _load_notices(
        self,
        *,
        days: int = 30,
        site_id: Optional[str] = None,
        agri_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self._agri._load_docs(days=days, site_id=site_id, agri_only=agri_only)

    def analyze_collected_data(
        self,
        *,
        days: int = 30,
        site_id: Optional[str] = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = f"{days}:{site_id or 'all'}"
        now = time.monotonic()
        if not refresh:
            cached = _analysis_cache.get(cache_key)
            if cached and now - cached[0] < _ANALYSIS_CACHE_TTL_SECONDS:
                return cached[1]

        docs = self._load_notices(days=days, site_id=site_id, agri_only=False)
        raw_leads: list[dict[str, Any]] = []
        excluded_count = 0
        for doc in docs:
            lead = _lead_from_doc(doc)
            if lead:
                raw_leads.append(lead)
            elif match_product_lines(doc) or doc_is_agri_related(doc):
                excluded_count += 1

        leads = deduplicate_leads(raw_leads)
        summary = self._build_summary(leads, excluded_count=excluded_count)
        report_date = app_now().strftime("%Y-%m-%d")

        result: dict[str, Any] = {
            "report_date": report_date,
            "days": days,
            "summary": summary,
            "leads": leads,
            "excluded_count": excluded_count,
            "total_notices_scanned": len(docs),
            "analyzed_at": app_now().isoformat(),
            "agent_status": "ready",
        }

        _analysis_cache[cache_key] = (now, result)
        self._persist_cache(result)
        return result

    def _build_summary(
        self,
        leads: list[dict[str, Any]],
        *,
        excluded_count: int = 0,
    ) -> dict[str, Any]:
        s_count = sum(1 for l in leads if l.get("opportunity_level") == "S")
        a_count = sum(1 for l in leads if l.get("opportunity_level") == "A")
        pending = sum(1 for l in leads if l.get("verification_status") == "pending")
        by_stage: dict[str, int] = {}
        by_product: dict[str, int] = {}
        for lead in leads:
            stage = lead.get("project_stage") or "其他"
            by_stage[stage] = by_stage.get(stage, 0) + 1
            for pm in lead.get("product_matches") or []:
                pl = pm.get("product_line") or ""
                by_product[pl] = by_product.get(pl, 0) + 1
        return {
            "new_count": len(leads),
            "updated_count": 0,
            "s_count": s_count,
            "a_count": a_count,
            "high_value_count": s_count + a_count,
            "pending_verification_count": pending,
            "b_count": sum(1 for l in leads if l.get("opportunity_level") == "B"),
            "c_count": sum(1 for l in leads if l.get("opportunity_level") == "C"),
            "excluded_count": excluded_count,
            "by_stage": by_stage,
            "by_product_line": by_product,
        }

    def get_dashboard_summary(
        self,
        *,
        days: int = 30,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        analysis = self.analyze_collected_data(days=days, site_id=site_id)
        return {
            "report_date": analysis["report_date"],
            "days": days,
            "summary": analysis["summary"],
            "analyzed_at": analysis["analyzed_at"],
            "total_notices_scanned": analysis["total_notices_scanned"],
            "agent_status": analysis["agent_status"],
        }

    def get_leads(
        self,
        *,
        days: int = 30,
        site_id: Optional[str] = None,
        stage: Optional[str] = None,
        product_line: Optional[str] = None,
        level: Optional[str] = None,
        verification: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        analysis = self.analyze_collected_data(days=days, site_id=site_id)
        items = list(analysis.get("leads") or [])
        if stage:
            items = [l for l in items if l.get("project_stage") == stage]
        if product_line:
            items = [
                l
                for l in items
                if any(
                    pm.get("product_line") == product_line
                    for pm in (l.get("product_matches") or [])
                )
            ]
        if level:
            items = [l for l in items if l.get("opportunity_level") == level.upper()]
        if verification:
            items = [l for l in items if l.get("verification_status") == verification]
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "days": days,
            "total": total,
            "items": page,
            "summary": analysis.get("summary"),
            "filters": {
                "stage": stage,
                "product_line": product_line,
                "level": level,
                "verification": verification,
            },
        }

    def generate_daily_brief(
        self,
        *,
        days: int = 30,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        analysis = self.analyze_collected_data(days=days, site_id=site_id)
        summary = analysis.get("summary") or {}
        leads = analysis.get("leads") or []
        date_str = analysis.get("report_date") or app_now().strftime("%Y-%m-%d")

        priority = [l for l in leads if l.get("opportunity_level") in ("S", "A")][:5]
        others = [l for l in leads if l.get("opportunity_level") not in ("S", "A")][:10]
        pending = [l for l in leads if l.get("verification_status") == "pending"][:8]

        lines = [
            f"# 数字农业线索日报｜{date_str}",
            "",
            "## 今日概览",
            f"- 新增：{summary.get('new_count', 0)}",
            f"- 重大更新：{summary.get('updated_count', 0)}",
            f"- S/A级：{summary.get('high_value_count', 0)}",
            f"- 待人工核验：{summary.get('pending_verification_count', 0)}",
            "",
            "## 优先跟进",
        ]
        if priority:
            for i, lead in enumerate(priority, 1):
                budget = lead.get("budget_display") or (
                    f"{lead['budget_amount_yuan'] / 10000:.0f}万元"
                    if lead.get("budget_amount_yuan")
                    else "预算待确认"
                )
                products = "、".join(lead.get("construction_scope") or [])
                evidence = (lead.get("evidence_quotes") or ["—"])[0]
                lines.extend(
                    [
                        f"{i}. {lead.get('project_name', '—')}｜{lead.get('region_display', '—')}｜"
                        f"{lead.get('project_stage', '—')}｜{budget}",
                        f"   - 匹配：{products or '—'}",
                        f"   - 评分：{lead.get('opportunity_level')}级 {lead.get('opportunity_score')}分",
                        f"   - 证据：{evidence}",
                        f"   - 建议：{lead.get('recommended_action', '—')}",
                        f"   - 原文：{lead.get('source_url') or '—'}",
                        "",
                    ]
                )
        else:
            lines.append("暂无 S/A 级线索。")
            lines.append("")

        lines.append("## 其他有效线索")
        if others:
            for lead in others:
                lines.append(
                    f"- [{lead.get('opportunity_level')} {lead.get('opportunity_score')}分] "
                    f"{lead.get('project_name', '—')}｜{lead.get('project_stage', '—')}｜"
                    f"{lead.get('region_display', '—')}"
                )
        else:
            lines.append("暂无其他有效线索。")
        lines.append("")

        lines.append("## 待核验线索")
        if pending:
            for lead in pending:
                missing = "、".join(lead.get("missing_fields") or []) or "官方原文"
                lines.append(
                    f"- {lead.get('project_name', '—')}：缺失 {missing}，建议回溯 P0 官方来源"
                )
        else:
            lines.append("暂无待核验线索。")
        lines.append("")

        lines.append("## 已排除结果")
        lines.append(
            f"共排除 {summary.get('excluded_count', 0)} 条（纯土建/农资/办公等无关采购）。"
        )

        markdown = "\n".join(lines)
        return {
            "report_date": date_str,
            "markdown": markdown,
            "summary": summary,
            "priority_count": len(priority),
        }

    def get_page_context(
        self,
        *,
        days: int = 30,
        refresh: bool = False,
    ) -> dict[str, Any]:
        analysis = self.analyze_collected_data(days=days, refresh=refresh)
        leads = analysis.get("leads") or []
        summary = analysis.get("summary") or {}
        brief = self.generate_daily_brief(days=days)
        priority_leads = [l for l in leads if l.get("opportunity_level") in ("S", "A")][:8]
        stages = list((load_biz_clue_keywords().get("stages") or []))
        product_lines = list((load_biz_clue_keywords().get("product_lines") or {}).keys())
        return {
            "analysis": analysis,
            "summary": summary,
            "leads": leads[:30],
            "priority_leads": priority_leads,
            "daily_brief_markdown": brief.get("markdown") or "",
            "stages": [s.get("name") for s in stages],
            "product_lines": product_lines,
            "analyzed_at": analysis.get("analyzed_at"),
            "agent_status": analysis.get("agent_status"),
            "total_notices_scanned": analysis.get("total_notices_scanned"),
            "config_version": get_config_version(),
            "config_label": f"基于 bizclue 配置 v{get_config_version()}",
            "days": days,
        }

    def _persist_cache(self, result: dict[str, Any]) -> None:
        try:
            ANALYSIS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            slim = {
                "report_date": result.get("report_date"),
                "analyzed_at": result.get("analyzed_at"),
                "summary": result.get("summary"),
                "lead_count": len(result.get("leads") or []),
            }
            ANALYSIS_CACHE_PATH.write_text(
                json.dumps(slim, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


def get_biz_clue_service() -> BizClueService:
    global _service
    if _service is None:
        _service = BizClueService()
    return _service


__all__ = [
    "BizClueService",
    "compute_opportunity_score",
    "deduplicate_leads",
    "get_biz_clue_service",
    "identify_project_stage",
    "load_biz_clue_keywords",
    "load_biz_clue_prompt",
    "match_product_lines",
]
