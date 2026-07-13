"""招标公告智慧农业相关性分类（关键词 + 可选 LLM，失败不阻断爬取）。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.core.models import BidNotice

logger = logging.getLogger(__name__)

_LAST_CALL_AT: float = 0.0
_RATE_LIMIT_SECONDS = 0.5

AGRI_MATCH_KEYWORDS: tuple[str, ...] = (
    "智慧农业",
    "数字农业",
    "农业物联网",
    "精准农业",
    "农业大数据",
    "智慧大棚",
    "农机智能化",
    "智慧农场",
    "数字乡村",
    "农业数字化",
    "智能灌溉",
    "农业传感器",
    "农业无人机",
    "农产品溯源",
    "农业云平台",
    "农业SaaS",
    "农业 SaaS",
    "智能农机",
    "遥感",
    "卫星遥感",
    "AI识别",
    "病虫害识别",
    "水肥一体化",
    "智慧种植",
    "农业信息化",
    "温室大棚",
    "农机",
    "三农",
    "乡村振兴",
    "畜牧数字化",
    "智慧渔业",
    "农产品供应链",
    "农业ERP",
)

# 技术/产品类型关键词映射
AGRI_TECH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "物联网": ("农业物联网", "农业传感器", "智能灌溉", "水肥一体化"),
    "遥感": ("遥感", "卫星遥感"),
    "AI识别": ("AI识别", "病虫害识别", "农业大数据"),
    "无人机": ("农业无人机", "无人机"),
    "云平台": ("农业云平台", "农业SaaS", "农业 SaaS", "农业ERP"),
    "智能农机": ("农机智能化", "智能农机", "农机"),
}

# 应用场景关键词映射
AGRI_SCENE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "种植": ("智慧种植", "智慧大棚", "温室大棚", "精准农业", "智慧农场"),
    "畜牧": ("畜牧数字化", "智慧养殖"),
    "渔业": ("智慧渔业",),
    "供应链": ("农产品溯源", "农产品供应链"),
    "数字乡村": ("数字乡村", "乡村振兴", "三农"),
}

_AGRI_SYSTEM_PROMPT = (
    "你是政府采购与农业农村项目分析助手。"
    "根据公告标题与正文，判断该招标/采购项目是否与智慧农业、数字农业领域相关。"
    "相关范围包括但不限于：智慧农业平台、农业物联网、精准农业、农业大数据、"
    "智慧大棚/温室、农机智能化、智慧农场、数字乡村、智能灌溉、农业传感器、"
    "农业无人机、遥感监测、AI病虫害识别、农产品溯源、农业信息化系统、农业SaaS等。"
    "若仅为普通农资/土建/办公采购且未涉及上述内容，判为非农业数字化相关。"
    "只输出合法 JSON，不要编造正文中不存在的信息。"
)

_AGRI_USER_TEMPLATE = {
    "task": (
        "判断以下招标/采购公告是否与智慧农业/数字农业相关。"
        "返回 JSON 对象，字段："
        "is_agri_related (boolean), "
        "confidence (0-1 浮点数), "
        "reason (string, 50字内简要理由), "
        "tags (string 数组，如 [\"农业物联网\",\"智慧大棚\"]，无则 []), "
        "tech_types (string 数组，技术类型如 物联网/遥感/AI识别/无人机/云平台/智能农机，无则 []), "
        "application_scenes (string 数组，应用场景如 种植/畜牧/渔业/供应链/数字乡村，无则 []), "
        "notice_type (string，招标/政策/新闻/其他 之一)"
    ),
}


class AgriClassification(BaseModel):
    """智慧农业分类结果。"""

    is_agri_related: Optional[bool] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason: Optional[str] = None
    tags: Optional[list[str]] = None
    tech_types: Optional[list[str]] = None
    application_scenes: Optional[list[str]] = None
    notice_type: Optional[str] = None


def infer_agri_tech_types(doc: dict[str, Any]) -> list[str]:
    """从文档推断数字农业技术/产品类型。"""
    text = "\n".join(
        str(doc.get(k) or "") for k in ("title", "content_text", "key_summary", "category")
    )
    found: list[str] = []
    for tech, kws in AGRI_TECH_KEYWORDS.items():
        if any(kw in text for kw in kws):
            found.append(tech)
    return found


def infer_agri_application_scenes(doc: dict[str, Any]) -> list[str]:
    """从文档推断农业应用场景。"""
    text = "\n".join(
        str(doc.get(k) or "") for k in ("title", "content_text", "key_summary", "category")
    )
    found: list[str] = []
    for scene, kws in AGRI_SCENE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            found.append(scene)
    return found


def infer_agri_notice_type(doc: dict[str, Any]) -> str:
    """推断公告类型：招标/政策/新闻/其他。"""
    category = str(doc.get("category") or "")
    title = str(doc.get("title") or "")
    haystack = f"{category} {title}"
    if any(x in haystack for x in ("招标", "采购", "投标", "竞争性磋商", "询价")):
        return "招标"
    if any(x in haystack for x in ("政策", "通知", "办法", "意见", "规划")):
        return "政策"
    if any(x in haystack for x in ("新闻", "资讯", "动态", "报道")):
        return "新闻"
    return "其他"


def agri_relevance_score(doc: dict[str, Any]) -> float:
    """数字农业相关度评分 0-1（关键词 + 入库字段）。"""
    if doc.get("is_agri_related") is False:
        return 0.0
    conf = doc.get("agri_confidence")
    if isinstance(conf, (int, float)):
        return max(0.0, min(1.0, float(conf)))
    kws = matched_agri_keywords(doc)
    if not kws:
        return 0.0 if doc.get("is_agri_related") is False else 0.3
    return min(1.0, 0.5 + len(kws) * 0.08)


def matched_agri_keywords(doc: dict[str, Any]) -> list[str]:
    """返回文档中命中的农业关键词。"""
    text_parts = [
        doc.get("title") or "",
        doc.get("content_text") or "",
        doc.get("key_summary") or "",
        doc.get("category") or "",
    ]
    text = "\n".join(str(p) for p in text_parts)
    if not text.strip():
        return []
    return [kw for kw in AGRI_MATCH_KEYWORDS if kw in text]


def is_agri_related_by_keywords(doc: dict[str, Any]) -> bool:
    return bool(matched_agri_keywords(doc))


def is_agri_related(doc: dict[str, Any]) -> bool:
    """公告是否与智慧农业相关（供洞察服务与测试使用）。"""
    return doc_is_agri_related(doc)


def score_agri_opportunity(doc: dict[str, Any]) -> dict[str, Any]:
    """农业招标商机评分（关键词 + 预算/类别加权）。"""
    kws = matched_agri_keywords(doc)
    score = 0
    if kws:
        score += min(40, 10 + len(kws) * 6)
    title = (doc.get("title") or "").lower()
    if any(x in title for x in ("平台", "系统", "建设", "改造", "升级")):
        score += 12
    category = str(doc.get("category") or "")
    if "招标" in category:
        score += 8
    budget = str(doc.get("budget_amount") or doc.get("amount_display") or "")
    digits = "".join(ch for ch in budget if ch.isdigit())
    if digits:
        try:
            amount = int(digits)
            if amount >= 1000:
                score += 20
            elif amount >= 300:
                score += 12
            elif amount >= 100:
                score += 6
        except ValueError:
            pass
    if doc.get("region"):
        score += 4
    score = max(0, min(100, score))
    if score >= 60:
        tier = "high"
    elif score >= 35:
        tier = "medium"
    else:
        tier = "low"
    return {
        "score": score,
        "tier": tier,
        "keywords": kws,
    }


def doc_is_agri_related(doc: dict[str, Any]) -> bool:
    """优先读入库字段，未标注时回退关键词匹配。"""
    rel = doc.get("is_agri_related")
    if rel is True:
        return True
    if rel is False:
        return False
    return is_agri_related_by_keywords(doc)


def is_agri_classify_llm_enabled() -> bool:
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.agri_classify_llm:
        return False
    return bool(settings.openai_api_key)


def is_agri_classify_enabled() -> bool:
    """关键词分类默认可用；LLM 为可选增强。"""
    return bool(AGRI_MATCH_KEYWORDS) or is_agri_classify_llm_enabled()


def is_agri_classify_available() -> bool:
    return is_agri_classify_enabled()


def classify_agri_by_keywords(
    title: str,
    content_text: str,
    *,
    key_summary: Optional[str] = None,
) -> AgriClassification:
    haystack = "\n".join(
        part for part in (title or "", key_summary or "", content_text or "") if part.strip()
    )
    if not haystack.strip():
        return AgriClassification()

    doc = {"title": title, "content_text": content_text, "key_summary": key_summary}
    matched = matched_agri_keywords(doc)
    if matched:
        return AgriClassification(
            is_agri_related=True,
            confidence=0.8,
            reason=f"关键词匹配: {', '.join(matched[:4])}",
            tags=matched[:6],
            tech_types=infer_agri_tech_types(doc),
            application_scenes=infer_agri_application_scenes(doc),
            notice_type=infer_agri_notice_type(doc),
        )
    return AgriClassification(
        is_agri_related=False,
        confidence=0.6,
        reason="未匹配智慧农业相关关键词",
        tags=[],
        notice_type=infer_agri_notice_type(doc),
    )


def _throttle() -> None:
    global _LAST_CALL_AT
    now = time.monotonic()
    elapsed = now - _LAST_CALL_AT
    if _LAST_CALL_AT > 0 and elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _LAST_CALL_AT = time.monotonic()


def classify_agri_notice(
    title: str,
    content_text: str,
    key_summary: Optional[str] = None,
    *,
    rate_limit: bool = False,
) -> AgriClassification:
    if not is_agri_classify_llm_enabled():
        return AgriClassification()

    from src.core.config import get_settings

    settings = get_settings()
    title = (title or "").strip()
    content_text = (content_text or "").strip()
    if not title and not content_text:
        return AgriClassification()

    preview = content_text[:6000] if len(content_text) > 6000 else content_text
    summary = (key_summary or "").strip()[:500]
    user_payload = {
        **_AGRI_USER_TEMPLATE,
        "title": title[:300],
        "key_summary": summary or None,
        "content": preview or None,
    }

    if rate_limit:
        _throttle()

    import urllib.error
    import urllib.request

    base_url = (settings.llm_base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    model = settings.llm_model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    url = f"{base_url}/v1/chat/completions" if base_url else "https://api.openai.com/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _AGRI_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning(
            "农业分类 LLM HTTP 错误 %s: %s",
            e.code,
            e.read().decode("utf-8", errors="replace")[:200],
        )
        return AgriClassification()
    except Exception as e:
        logger.warning("农业分类 LLM 调用失败: %s", e)
        return AgriClassification()

    content = payload["choices"][0]["message"]["content"].strip()
    if "```" in content:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            content = content[start:end]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("农业分类 LLM 返回非 JSON")
        return AgriClassification()

    is_related = data.get("is_agri_related")
    if is_related is not None and not isinstance(is_related, bool):
        is_related = bool(is_related)

    confidence = data.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = None

    reason = data.get("reason")
    if reason is not None:
        reason = str(reason).strip() or None

    tags_raw = data.get("tags")
    tags: Optional[list[str]] = None
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    elif tags_raw:
        tags = [str(tags_raw).strip()]

    def _parse_str_list(key: str) -> Optional[list[str]]:
        raw = data.get(key)
        if isinstance(raw, list):
            items = [str(t).strip() for t in raw if str(t).strip()]
            return items or None
        if raw:
            return [str(raw).strip()]
        return None

    notice_type = data.get("notice_type")
    if notice_type is not None:
        notice_type = str(notice_type).strip() or None

    return AgriClassification(
        is_agri_related=is_related,
        confidence=confidence,
        reason=reason,
        tags=tags or None,
        tech_types=_parse_str_list("tech_types"),
        application_scenes=_parse_str_list("application_scenes"),
        notice_type=notice_type,
    )


def apply_agri_classification_to_notice(
    notice: BidNotice,
    *,
    rate_limit: bool = False,
    force: bool = False,
) -> BidNotice:
    """将农业分类结果写入 BidNotice；已分类且非 force 时跳过。"""
    if not force and notice.is_agri_related is not None:
        return notice

    text = (notice.content_text or "").strip()
    title = (notice.title or "").strip()
    if not text and not title:
        return notice

    result = classify_agri_by_keywords(title, text, key_summary=notice.key_summary)
    if is_agri_classify_llm_enabled() and (force or result.is_agri_related is None):
        llm_result = classify_agri_notice(title, text, notice.key_summary, rate_limit=rate_limit)
        if llm_result.is_agri_related is not None:
            result = llm_result
        elif result.is_agri_related is None:
            result = classify_agri_by_keywords(title, text, key_summary=notice.key_summary)

    notice.is_agri_related = result.is_agri_related
    notice.agri_confidence = result.confidence
    notice.agri_reason = result.reason
    notice.agri_tags = result.tags
    if result.is_agri_related is not None:
        notice.agri_classified_at = datetime.now(timezone.utc)
    return notice


def agri_tags_for_tagged_documents(notice: BidNotice) -> list[str]:
    """合并固定前缀与公告 agri_tags，供 tagged_documents 检索。"""
    tags: list[str] = ["agri"]
    seen = {"agri"}
    for raw in notice.agri_tags or []:
        tag = str(raw).strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags
