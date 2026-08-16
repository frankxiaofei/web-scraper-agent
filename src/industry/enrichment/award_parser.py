"""中标关系解析（Phase 2 P2-T-004）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.core.models import BidNotice, NoticeCategory

_WINNER_LABELS = (
    "中标供应商",
    "中标单位",
    "中标人",
    "成交供应商",
    "成交单位",
    "供应商名称",
    "中标（成交）供应商",
    "中标（成交）单位",
)
_ISSUER_LABELS = (
    "采购人",
    "招标人",
    "采购单位",
    "项目业主",
    "发包人",
)

_WINNER_TITLE_RE = re.compile(
    r"(?:中标|成交)(?:供应商|单位|人|结果)?[：:\s]*([^\n\r，,；;。]{2,80}?)"
    r"(?:为|是|[:：]\s*)?([^\n\r，,；;。]{2,80}?)(?:公司|集团|中心|院|所|局|部|处|站|厂|店|社|队|厅|委|办|馆|校|大学|学院|医院|银行|合作社|农场|场|公司)?",
    re.I,
)
_TITLE_WIN_RE = re.compile(
    r"关于(.{2,60}?)(?:中标|成交)(?:公告|结果|公示)?",
    re.I,
)
_COMPANY_SUFFIX = re.compile(
    r"(有限责任公司|股份有限公司|有限公司|集团公司|集团|公司|中心|研究院|研究所|大学|学院|医院|银行|合作社)$"
)


@dataclass
class AwardParseResult:
    winner_name: str | None = None
    issuer_name: str | None = None
    confidence: float = 0.0
    method: str = "none"

    def to_dict(self) -> dict:
        return {
            "winner_name": self.winner_name,
            "issuer_name": self.issuer_name,
            "confidence": self.confidence,
            "method": self.method,
        }


def _clean_company(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"\s+", "", name.strip())
    cleaned = re.sub(r"[（(].*?[）)]", "", cleaned).strip()
    if len(cleaned) < 2 or len(cleaned) > 120:
        return None
    if cleaned in {"无", "暂无", "—", "-", "null", "None"}:
        return None
    return cleaned


def _extract_labeled(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        patterns = [
            rf"{re.escape(label)}[：:\s]+([^\n\r，,；;]+)",
            rf"{re.escape(label)}\s*[:：]\s*([^\n\r]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                val = _clean_company(m.group(1).strip())
                if val:
                    return val
    return None


def _looks_like_company(name: str) -> bool:
    if _COMPANY_SUFFIX.search(name):
        return True
    if re.search(r"(政府|委员会|管理局|办公室|中心|学校|医院|银行|研究院)", name):
        return True
    return len(name) >= 4 and not re.search(r"^\d+$", name)


def parse_award_relations(notice: BidNotice) -> AwardParseResult:
    """从公告标题/正文解析采购方→中标方关系。"""
    is_win = notice.category == NoticeCategory.WIN or "中标" in (notice.title or "") or "成交" in (
        notice.title or ""
    )
    if not is_win:
        return AwardParseResult(method="skipped_not_win")

    text = "\n".join(
        x
        for x in [
            notice.title or "",
            notice.content_text or "",
            notice.key_summary or "",
        ]
        if x
    )
    winner: Optional[str] = None
    issuer: Optional[str] = None
    method = "none"
    confidence = 0.0

    if notice.tender_party and _looks_like_company(notice.tender_party.strip()):
        winner = _clean_company(notice.tender_party)
        if winner:
            method = "tender_party_field"
            confidence = 0.85

    if not winner and text:
        winner = _extract_labeled(text, _WINNER_LABELS)
        if winner:
            method = "content_label"
            confidence = 0.91

    if not winner and notice.title:
        m = _WINNER_TITLE_RE.search(notice.title)
        if m:
            candidate = _clean_company(m.group(2) or m.group(1))
            if candidate and _looks_like_company(candidate):
                winner = candidate
                method = "title_regex"
                confidence = 0.75

    if notice.purchaser:
        issuer = _clean_company(notice.purchaser)
    elif text:
        issuer = _extract_labeled(text, _ISSUER_LABELS)

    if not issuer and notice.title:
        m = _TITLE_WIN_RE.search(notice.title)
        if m:
            issuer = _clean_company(m.group(1))

    if winner and confidence == 0.0:
        confidence = 0.6
        method = method or "fallback"

    return AwardParseResult(
        winner_name=winner,
        issuer_name=issuer,
        confidence=confidence if winner else 0.0,
        method=method,
    )
