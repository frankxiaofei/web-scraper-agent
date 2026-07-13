"""招标公告 BIM 相关性分类（LLM，失败不阻断爬取）。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from src.core.models import BidNotice

logger = logging.getLogger(__name__)

_LAST_CALL_AT: float = 0.0
_RATE_LIMIT_SECONDS = 0.5

_BIM_SYSTEM_PROMPT = (
    "你是政府采购与工程建设招标分析助手。"
    "根据公告标题与正文，判断该招标/采购项目是否与 BIM 及相关数字化建造领域相关。"
    "相关范围包括但不限于：BIM（建筑信息模型）、CIM（城市信息模型）、数字孪生、"
    "Revit/ArchiCAD/Bentley 等 BIM 建模、BIM 咨询/设计/施工/运维、BIM 平台或管理系统、"
    "全生命周期 BIM 应用、智慧工地中的 BIM 模块等。"
    "若仅为普通土建/装修/设备采购且未涉及上述内容，判为非 BIM 相关。"
    "只输出合法 JSON，不要编造正文中不存在的信息。"
)

_BIM_KEYWORDS: tuple[str, ...] = (
    "BIM",
    "BIM模型",
    "建筑信息模型",
    "Revit",
    "IFC",
    "数字孪生",
    "CIM",
    "城市信息模型",
    "ArchiCAD",
    "Bentley",
    "BIM平台",
    "BIM咨询",
    "BIM设计",
    "BIM施工",
    "BIM运维",
    "智慧工地",
    "构件库",
    "Navisworks",
    "Tekla",
)

_BIM_USER_TEMPLATE = {
    "task": (
        "判断以下招标/采购公告是否与 BIM 及相关领域相关。"
        "返回 JSON 对象，字段："
        "is_bim_related (boolean), "
        "confidence (0-1 浮点数), "
        "reason (string, 50字内简要理由), "
        "tags (string 数组，如 [\"BIM建模\",\"数字孪生\"]，无则 [])"
    ),
}


class BIMClassification(BaseModel):
    """BIM 分类结果。"""

    is_bim_related: Optional[bool] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason: Optional[str] = None
    tags: Optional[list[str]] = None


def is_bim_classify_enabled() -> bool:
    """是否启用 BIM LLM 分类（需 API Key）。"""
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.bim_classify_llm:
        return False
    return bool(settings.openai_api_key)


def is_bim_classify_available() -> bool:
    """LLM 或关键词 fallback 至少一种可用。"""
    return is_bim_classify_enabled() or bool(_BIM_KEYWORDS)


def classify_bim_by_keywords(
    title: str,
    content_text: str,
    *,
    key_summary: Optional[str] = None,
) -> BIMClassification:
    """关键词规则判定（LLM 不可用或失败时的 fallback）。"""
    haystack = "\n".join(
        part for part in (title or "", key_summary or "", content_text or "") if part.strip()
    )
    if not haystack.strip():
        return BIMClassification()

    matched: list[str] = []
    for kw in _BIM_KEYWORDS:
        if kw.lower() in haystack.lower() or kw in haystack:
            matched.append(kw)
    if matched:
        return BIMClassification(
            is_bim_related=True,
            confidence=0.75,
            reason=f"关键词匹配: {', '.join(matched[:4])}",
            tags=matched[:6],
        )
    return BIMClassification(
        is_bim_related=False,
        confidence=0.55,
        reason="未匹配 BIM 相关关键词",
        tags=[],
    )


def _throttle() -> None:
    global _LAST_CALL_AT
    now = time.monotonic()
    elapsed = now - _LAST_CALL_AT
    if _LAST_CALL_AT > 0 and elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _LAST_CALL_AT = time.monotonic()


def classify_bim_notice(
    title: str,
    content_text: str,
    key_summary: Optional[str] = None,
    *,
    rate_limit: bool = False,
) -> BIMClassification:
    """调用 LLM 判断公告是否与 BIM 相关；失败时返回 is_bim_related=None。"""
    if not is_bim_classify_enabled():
        return BIMClassification()

    from src.core.config import get_settings

    settings = get_settings()
    title = (title or "").strip()
    content_text = (content_text or "").strip()
    if not title and not content_text:
        return BIMClassification()

    preview = content_text[:6000] if len(content_text) > 6000 else content_text
    summary = (key_summary or "").strip()[:500]
    user_payload = {
        **_BIM_USER_TEMPLATE,
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
                {"role": "system", "content": _BIM_SYSTEM_PROMPT},
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
            "BIM 分类 LLM HTTP 错误 %s: %s",
            e.code,
            e.read().decode("utf-8", errors="replace")[:200],
        )
        return BIMClassification()
    except Exception as e:
        logger.warning("BIM 分类 LLM 调用失败: %s", e)
        return BIMClassification()

    content = payload["choices"][0]["message"]["content"].strip()
    if "```" in content:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            content = content[start:end]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("BIM 分类 LLM 返回非 JSON")
        return BIMClassification()

    is_related = data.get("is_bim_related")
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

    return BIMClassification(
        is_bim_related=is_related,
        confidence=confidence,
        reason=reason,
        tags=tags or None,
    )


def apply_bim_classification_to_notice(
    notice: BidNotice,
    *,
    rate_limit: bool = False,
    force: bool = False,
) -> BidNotice:
    """将 BIM 分类结果写入 BidNotice；已分类且非 force 时跳过。"""
    if not force and notice.is_bim_related is not None:
        return notice

    text = (notice.content_text or "").strip()
    title = (notice.title or "").strip()
    if not text and not title:
        return notice

    result = classify_bim_notice(
        title,
        text,
        notice.key_summary,
        rate_limit=rate_limit,
    )
    if result.is_bim_related is None:
        result = classify_bim_by_keywords(title, text, key_summary=notice.key_summary)
    notice.is_bim_related = result.is_bim_related
    notice.bim_confidence = result.confidence
    notice.bim_reason = result.reason
    notice.bim_tags = result.tags
    if result.is_bim_related is not None:
        notice.bim_classified_at = datetime.now(timezone.utc)
    return notice
