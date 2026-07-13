"""工程大模型应用招标简报：筛选 LLM/AI 相关标讯并私信推送。"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Union

from src.core.config import get_settings
from src.core.feishu_webhook import build_feishu_card_header
from src.core.timezone_utils import APP_TZ
from src.web.data_service import doc_publish_timestamp, doc_source_id, get_data_service
from src.web.stats_service import _load_site_metadata, _with_pct

logger = logging.getLogger(__name__)

TZ = APP_TZ
DateInput = Union[date, datetime, str, None]

# 工程大模型应用相关关键词（标题 / 摘要 / 正文）
_LLM_KEYWORDS: tuple[str, ...] = (
    "大模型",
    "LLM",
    "llm",
    "人工智能",
    "智能体",
    "AIGC",
    "GPT",
    "gpt",
    "ChatGPT",
    "深度学习",
    "机器学习",
    "数字孪生",
    "知识图谱",
    "自然语言",
    "NLP",
    "工程大模型",
    "垂直大模型",
    "行业大模型",
    "生成式",
    "Copilot",
    "copilot",
    "AI应用",
    "AI平台",
    "AI中台",
    "模型应用",
    "智能问答",
    "智能助手",
)

_ENGINEERING_KEYWORDS: tuple[str, ...] = (
    "工程",
    "施工",
    "建设",
    "水利",
    "电力",
    "交通",
    "市政",
    "勘察",
    "设计",
    "BIM",
    "bim",
)


def is_engineering_llm_candidate(doc: dict[str, Any]) -> bool:
    """判断公告是否属于工程大模型应用相关标讯。"""
    parts = [
        doc.get("title") or "",
        doc.get("key_summary") or "",
        (doc.get("content") or "")[:2000],
    ]
    text = " ".join(parts)
    if not text.strip():
        return False
    if "工程大模型" in text:
        return True
    text_lower = text.lower()
    has_llm = any(kw.lower() in text_lower for kw in _LLM_KEYWORDS)
    if not has_llm:
        return False
    has_eng = any(kw in text for kw in _ENGINEERING_KEYWORDS)
    return has_eng or "大模型" in text or "智能体" in text


def _parse_target_date(value: DateInput) -> date:
    if value is None:
        return datetime.now(TZ).date()
    if isinstance(value, datetime):
        return value.astimezone(TZ).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(TZ).date()
    return date.fromisoformat(text[:10])


def _date_range(target: date, days: int) -> tuple[datetime, datetime]:
    start = datetime.combine(target, time.min, tzinfo=TZ)
    end = start + timedelta(days=max(1, days))
    return start, end


def _engineering_notice_item(doc: dict[str, Any]) -> dict[str, Any]:
    from src.core.url_utils import resolve_notice_detail_url
    from src.web.data_service import _format_dt, notice_content_href, notice_id_from_doc

    pub_ts = doc_publish_timestamp(doc)
    notice_id = notice_id_from_doc(doc)
    return {
        "id": notice_id,
        "content_href": notice_content_href(notice_id),
        "title": doc.get("title") or "",
        "url": resolve_notice_detail_url(
            doc.get("url") or doc.get("detail_url") or "",
            base_url=doc.get("source_url") or "",
        ),
        "source_site_id": doc_source_id(doc),
        "source_site_name": doc.get("source_site_name") or doc_source_id(doc),
        "publish_date_fmt": _format_dt(pub_ts) if pub_ts else "—",
        "key_summary": (doc.get("key_summary") or "")[:160],
    }


def _doc_in_range(doc: dict[str, Any], start: datetime, end: datetime) -> bool:
    ts = doc_publish_timestamp(doc, warn_fallback=True)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ)
    else:
        ts = ts.astimezone(TZ)
    return start <= ts < end


def load_engineering_llm_docs(*, days: int = 7) -> list[dict[str, Any]]:
    """加载近 N 天工程大模型应用候选公告。"""
    from src.core.site_sync import load_enabled_sites

    days = max(1, min(days, 90))
    cutoff = datetime.now(TZ) - timedelta(days=days)
    enabled_ids = {s["id"] for s in load_enabled_sites() if s.get("id")}
    data = get_data_service()
    docs: list[dict[str, Any]] = []

    if data.data_source == "mongodb" and data._mongo_coll is not None:  # noqa: SLF001
        scope = data._mvp_source_filter_mongo(list(enabled_ids))  # noqa: SLF001
        llm_regex = "|".join(re.escape(kw) for kw in _LLM_KEYWORDS[:12])
        query = {
            "$and": [
                scope,
                {
                    "$or": [
                        {"title": {"$regex": llm_regex, "$options": "i"}},
                        {"key_summary": {"$regex": llm_regex, "$options": "i"}},
                    ]
                },
            ]
        }
        from src.web.data_service import NOTICE_MONGO_SORT

        for doc in data._mongo_coll.find(query).sort(NOTICE_MONGO_SORT):  # noqa: SLF001
            ts = doc_publish_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            if is_engineering_llm_candidate(doc):
                docs.append(doc)
        return docs

    for doc in data._load_jsonl():  # noqa: SLF001
        if not data._in_mvp_scope(doc):  # noqa: SLF001
            continue
        ts = doc_publish_timestamp(doc)
        if ts is not None and ts < cutoff:
            continue
        if is_engineering_llm_candidate(doc):
            docs.append(doc)
    return docs


def build_engineering_llm_brief(
    target_date: DateInput = None,
    *,
    days: int = 1,
    top_n: int = 10,
) -> dict[str, Any]:
    """生成工程大模型应用简报。"""
    target = _parse_target_date(target_date)
    days = max(1, min(days, 31))
    top_n = max(1, min(top_n, 30))
    start, end = _date_range(target, days)

    lookback = min(90, max(7, (datetime.now(TZ).date() - target).days + days + 1))
    all_docs = load_engineering_llm_docs(days=lookback)
    docs = [d for d in all_docs if _doc_in_range(d, start, end)]
    docs.sort(key=lambda d: doc_publish_timestamp(d) or datetime.min.replace(tzinfo=TZ), reverse=True)

    site_meta = _load_site_metadata()
    by_site: Counter[str] = Counter()
    for doc in docs:
        sid = doc_source_id(doc)
        name = site_meta.get(sid, {}).get("name") or sid or "未知站点"
        by_site[name] += 1

    top_items = [_engineering_notice_item(d) for d in docs[:top_n]]
    date_end = (target + timedelta(days=days - 1)).isoformat()
    return {
        "report_kind": "engineering_llm",
        "date": target.isoformat(),
        "date_end": date_end,
        "days": days,
        "count": len(docs),
        "by_site": _with_pct(dict(by_site.most_common(10)), len(docs)),
        "top_items": top_items,
    }


def build_engineering_llm_feishu_card(brief: dict[str, Any]) -> dict[str, Any]:
    """将工程大模型应用简报转为飞书 interactive card。"""
    days = int(brief.get("days") or 1)
    date_label = brief.get("date") or ""
    if days > 1 and brief.get("date_end"):
        date_label = f"{brief.get('date')} ~ {brief.get('date_end')}"
    title = f"工程大模型应用 {'日报' if days <= 1 else '简报'} {date_label}"

    summary_lines = [
        f"**统计区间**：{date_label}",
        f"**相关标讯**：{brief.get('count', 0)} 条",
    ]
    by_site = brief.get("by_site") or {}
    if by_site:
        site_parts = [f"{name} {cnt}条" for name, cnt in list(by_site.items())[:5]]
        summary_lines.append(f"**站点分布**：{' · '.join(site_parts)}")

    settings = get_settings()
    item_lines: list[str] = []
    for idx, item in enumerate(brief.get("top_items") or [], start=1):
        t = (item.get("title") or "无标题").replace("\n", " ")[:80]
        content_href = (item.get("content_href") or "").strip()
        if content_href:
            url = f"{settings.public_base_url.rstrip('/')}{content_href}"
        else:
            url = item.get("url") or ""
        site_name = item.get("source_site_name") or item.get("source_site_id") or ""
        line = f"**{idx}.** [{t}]({url})"
        if site_name:
            line += f" · {site_name}"
        item_lines.append(line)

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(summary_lines)},
        },
    ]
    if item_lines:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**重点标讯**\n" + "\n".join(item_lines),
                },
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看商机洞察"},
                    "type": "primary",
                    "url": f"{settings.public_base_url.rstrip('/')}/insights",
                }
            ],
        }
    )
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "<font color='grey'>仅院内环境可打开</font>",
            },
        }
    )

    return {
        "header": build_feishu_card_header(title, template="blue"),
        "elements": elements,
    }


def push_engineering_llm_report(
    *,
    feishu_user_ids: list[str],
    target_date: DateInput = None,
    days: int = 1,
    top_n: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """生成工程大模型应用简报并私信推送给指定飞书用户。"""
    user_ids = [u.strip() for u in feishu_user_ids if u and str(u).strip()]
    if not user_ids:
        return {"ok": False, "error": "feishu_user_ids 不能为空"}

    brief = build_engineering_llm_brief(target_date, days=days, top_n=top_n)
    card = build_engineering_llm_feishu_card(brief)
    payload = {"msg_type": "interactive", "card": card}

    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "dry_run": True,
            "brief": brief,
            "payload": payload,
            "feishu_user_ids": user_ids,
        }

    from src.core.feishu_push import send_feishu_interactive_card_to_users

    push_result = send_feishu_interactive_card_to_users(
        card,
        user_ids,
        dry_run=False,
    )
    return {
        **push_result,
        "brief": brief,
        "payload": payload,
        "feishu_user_ids": user_ids,
    }
