"""BIM 招标每日简报生成与飞书推送。"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Union

from src.core.config import get_settings
from src.core.timezone_utils import APP_TZ
from src.core.feishu_push import FeishuChannel, send_feishu_interactive_card, send_feishu_interactive_card_to_users
from src.core.feishu_webhook import FeishuWebhookClient, build_feishu_card_header, format_feishu_card_title
from src.core.notice_field_utils import (
    aggregate_structured_insights,
    extract_notice_fields_from_doc,
    parse_amount_yuan,
)
from src.web.bim_insights_service import (
    ANALYSIS_WINDOW_DAYS,
    BimInsightsService,
    _doc_source_id,
    _doc_timestamp,
    _notice_item,
    get_bim_insights_service,
)
from src.web.data_service import doc_publish_timestamp
from src.web.stats_service import _load_site_metadata, _with_pct

logger = logging.getLogger(__name__)

TZ = APP_TZ
DateInput = Union[date, datetime, str, None]

_KEY_SUMMARY_MAX_LEN = 100
_TENDER_PARTY_MAX_LEN = 40
_FEISHU_CARD_FOOTER = (
    "日报周一到周五每天8点推送，周报每周五晚上6点推送。"
    "如您遇到任何问题联系李晓飞（li_xf10）。"
)


def _clean_display_text(text: str, *, max_len: int | None = None) -> str:
    """压缩空白并可选截断展示文本。"""
    cleaned = re.sub(r"\s+", " ", str(text).strip())
    if max_len and len(cleaned) > max_len:
        return cleaned[:max_len].rstrip() + "…"
    return cleaned


def _format_amount_display(
    amount_raw: str | None = None,
    amount_yuan: float | None = None,
) -> str:
    """将金额统一为带单位的中文展示（万元 / 亿元 / 元）。"""
    if amount_raw:
        raw = _clean_display_text(amount_raw)
        if re.search(r"(万元|万|亿元|亿|元)", raw):
            return raw
        parsed = parse_amount_yuan(raw)
        if parsed is not None:
            amount_yuan = parsed

    if amount_yuan is None or amount_yuan <= 0:
        return ""

    if amount_yuan >= 1e8:
        val = amount_yuan / 1e8
        text = f"{val:.2f}".rstrip("0").rstrip(".")
        return f"{text} 亿元"
    if amount_yuan >= 1e4:
        val = amount_yuan / 1e4
        text = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{text} 万元"
    if amount_yuan == int(amount_yuan):
        return f"{int(amount_yuan)} 元"
    return f"{amount_yuan:.0f} 元"


def _format_bim_tags(tags: list[Any]) -> str:
    """将 BIM 标签格式化为可读文本（非 JSON）。"""
    labels: list[str] = []
    for tag in tags[:5]:
        if isinstance(tag, dict):
            label = tag.get("name") or tag.get("tag") or ""
        else:
            label = str(tag).strip()
        if label:
            labels.append(label)
    if not labels:
        return ""
    return "**标签**：" + " · ".join(labels)


def _brief_report_title(
    days: int, date_label: str, *, is_weekend_daily: bool = False
) -> str:
    if days >= 28:
        return f"商机招标周报 {date_label}"
    if is_weekend_daily:
        return f"商机招标日报 {date_label}（周末）"
    if days > 1:
        return f"商机招标简报 {date_label}"
    return f"商机招标日报 {date_label}"


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


def _format_window_datetime(dt: datetime) -> str:
    """格式化为 YYYY-MM-DD HH:MM（Asia/Shanghai）。"""
    local = dt.astimezone(TZ)
    return local.strftime("%Y-%m-%d %H:%M")


def _window_end_inclusive(end_exclusive: datetime) -> datetime:
    """将左闭右开窗口的结束时刻转为含边界展示（如 23:59）。"""
    return end_exclusive.astimezone(TZ) - timedelta(minutes=1)


def _format_window_label(
    start: datetime, end_exclusive: datetime, *, suffix: str = ""
) -> str:
    """生成带小时精度的统计区间文案。"""
    end_inclusive = _window_end_inclusive(end_exclusive)
    label = f"{_format_window_datetime(start)} ~ {_format_window_datetime(end_inclusive)}"
    return f"{label}{suffix}" if suffix else label


def resolve_daily_report_window(
    reference: DateInput = None,
) -> tuple[date, int, bool]:
    """解析 8:00 定时日报的统计窗口（Asia/Shanghai）。

    - 周一推送：上周末（上周六 00:00 ~ 上周日 23:59），2 天
    - 周二至周日推送：昨日单日
    """
    ref = _parse_target_date(reference)
    if ref.weekday() == 0:
        return ref - timedelta(days=2), 2, True
    return ref - timedelta(days=1), 1, False


def last_calendar_week_range(reference: DateInput = None) -> tuple[date, date]:
    """返回上一自然周（周一至周日）的起止日期（均为 date）。"""
    ref = _parse_target_date(reference)
    this_monday = ref - timedelta(days=ref.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def weekly_report_range(reference: DateInput = None) -> tuple[date, date]:
    """返回本自然周周报区间：当周周一至参考日（不超过周五）。"""
    ref = _parse_target_date(reference)
    this_monday = ref - timedelta(days=ref.weekday())
    this_friday = this_monday + timedelta(days=4)
    end = min(ref, this_friday)
    return this_monday, end


def month_report_range(reference: DateInput = None) -> tuple[date, date]:
    """返回近一月（滚动 30 天）起止：参考日往前 29 天至参考日（含）。"""
    ref = _parse_target_date(reference)
    return ref - timedelta(days=ANALYSIS_WINDOW_DAYS - 1), ref


def _datetime_range_inclusive(start: date, end: date) -> tuple[datetime, datetime]:
    dt_start = datetime.combine(start, time.min, tzinfo=TZ)
    dt_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=TZ)
    return dt_start, dt_end


def _count_docs_in_date_range(
    docs: list[dict[str, Any]], start: date, end: date
) -> int:
    dt_start, dt_end = _datetime_range_inclusive(start, end)
    return sum(1 for doc in docs if _doc_in_range(doc, dt_start, dt_end))


def compute_period_new_counts(
    reference: DateInput = None,
    *,
    insights_service: Optional[BimInsightsService] = None,
) -> dict[str, Any]:
    """统计本周/近一月（滚动 30 天）新增 BIM 候选（与周报相同的 bim_brief 口径与 publish_date 过滤）。"""
    ref = _parse_target_date(reference)
    week_start, week_end = weekly_report_range(ref)
    month_start, month_end = month_report_range(ref)

    svc = insights_service or get_bim_insights_service()
    lookback_days = min(90, max(31, (ref - month_start).days + 7))
    all_docs = svc.load_bim_report_docs(days=lookback_days)

    return {
        "week_new_count": _count_docs_in_date_range(all_docs, week_start, week_end),
        "week_range_start": week_start.isoformat(),
        "week_range_end": week_end.isoformat(),
        "month_new_count": _count_docs_in_date_range(all_docs, month_start, month_end),
        "month_range_start": month_start.isoformat(),
        "month_range_end": month_end.isoformat(),
    }


def _doc_in_range(doc: dict[str, Any], start: datetime, end: datetime) -> bool:
    # 按公告发布时间过滤；缺失时 doc_publish_timestamp 回退采集时间
    ts = doc_publish_timestamp(doc, warn_fallback=True)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ)
    else:
        ts = ts.astimezone(TZ)
    return start <= ts < end


_LLM_EXTRACT_TOP_N = 10


def _build_top_item_summary_from_fields(fields: dict[str, Any]) -> str:
    """从已抽取字段组装 Top 标讯摘要；缺失字段省略，每行一个字段。"""
    lines: list[str] = []

    amount_text = _format_amount_display(
        fields.get("contract_amount"),
        fields.get("amount_yuan"),
    )
    if amount_text:
        lines.append(f"合同金额：{amount_text}")

    tender = fields.get("tender_party")
    if tender:
        lines.append(f"招标方：{_clean_display_text(tender, max_len=_TENDER_PARTY_MAX_LEN)}")

    period = fields.get("project_period")
    if period:
        lines.append(f"项目周期：{period}")

    key = fields.get("key_content")
    if key:
        lines.append(f"招标关键内容：{key}")

    return "\n".join(lines)


def _build_top_item_summary(doc: dict[str, Any], *, use_llm: bool | None = None) -> str:
    """从结构化字段组装 Top 标讯摘要；缺失字段省略，每行一个字段。"""
    fields = extract_notice_fields_from_doc(doc, use_llm=use_llm)
    return _build_top_item_summary_from_fields(fields)


def _format_top_item_block(idx: int, item: dict[str, Any], detail_url: str) -> str:
    """单条 Top 标讯的飞书 markdown 块。"""
    title = _clean_display_text(item.get("title") or "无标题", max_len=80)
    settings = get_settings()
    content_href = (item.get("content_href") or "").strip()
    if content_href:
        url = f"{settings.public_base_url.rstrip('/')}{content_href}"
    else:
        url = item.get("url") or detail_url
    block_lines = [f"**{idx}.** [{title}]({url})"]

    tags_text = _format_bim_tags(item.get("bim_tags") or [])
    if tags_text:
        block_lines.append(tags_text)

    summary = (item.get("summary") or "").strip()
    if summary:
        for line in summary.split("\n"):
            block_lines.append(f"> {line}")

    return "\n".join(block_lines)


def _enrich_brief_item(doc: dict[str, Any], *, use_llm: bool | None = None) -> dict[str, Any]:
    """为简报条目补充结构化摘要（不含 BIM 判定理由与正文片段）。"""
    item = _notice_item(doc)
    item.pop("bim_reason", None)
    fields = extract_notice_fields_from_doc(doc, use_llm=use_llm)
    item["contract_amount"] = fields.get("contract_amount")
    item["project_period"] = fields.get("project_period")
    item["extraction_source"] = fields.get("extraction_source")
    item["summary"] = _build_top_item_summary_from_fields(fields)
    return item


def build_bim_daily_brief(
    target_date: DateInput = None,
    *,
    days: int = 1,
    top_n: int = 10,
    is_weekend_daily: bool = False,
    insights_service: Optional[BimInsightsService] = None,
) -> dict[str, Any]:
    """从 MongoDB/JSONL 查询指定日期窗口内的 BIM 标讯并生成简报。"""
    target = _parse_target_date(target_date)
    days = max(1, days)
    top_n = max(1, min(top_n, 30))
    start, end = _date_range(target, days)

    svc = insights_service or get_bim_insights_service()
    lookback_days = min(90, max(7, (datetime.now(TZ).date() - target).days + days + 1))
    all_docs = svc.load_bim_report_docs(days=lookback_days)
    docs = [d for d in all_docs if _doc_in_range(d, start, end)]
    docs.sort(key=lambda d: _doc_timestamp(d) or datetime.min.replace(tzinfo=TZ), reverse=True)

    yaml_names, yaml_companies = _load_site_metadata()
    site_counter: Counter[str] = Counter()
    site_names: dict[str, str] = {}
    amount_total = 0.0
    amount_count = 0

    for doc in docs:
        sid = _doc_source_id(doc)
        site_counter[sid] += 1
        site_names[sid] = doc.get("source_site_name") or yaml_names.get(sid) or sid
        item = _notice_item(doc)
        amount = parse_amount_yuan(item.get("budget_amount"))
        if amount is not None and amount > 0:
            amount_total += amount
            amount_count += 1

    total = len(docs)
    by_site = _with_pct(
        [
            {
                "id": sid,
                "name": site_names.get(sid) or yaml_names.get(sid) or sid,
                "company_name": yaml_companies.get(sid) or "",
                "count": cnt,
            }
            for sid, cnt in site_counter.most_common()
        ],
        total=total,
    )
    structured = aggregate_structured_insights(docs) if docs else {}
    top_items = [
        _enrich_brief_item(
            d,
            use_llm=True if idx < _LLM_EXTRACT_TOP_N else False,
        )
        for idx, d in enumerate(docs[:top_n])
    ]

    settings = get_settings()
    detail_url = f"{settings.bim_ui_url.rstrip('/')}/insights"
    window_end_inclusive = _window_end_inclusive(end)
    window_suffix = "（周末）" if is_weekend_daily else ""
    window_label = _format_window_label(start, end, suffix=window_suffix)

    return {
        "date": target.isoformat(),
        "date_end": (target + timedelta(days=days - 1)).isoformat(),
        "days": days,
        "is_weekend_daily": is_weekend_daily,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_start_label": _format_window_datetime(start),
        "window_end_label": _format_window_datetime(window_end_inclusive),
        "window_label": window_label,
        "bim_count": total,
        "top_items": top_items,
        "by_site": by_site,
        "amount_stats": {
            "disclosed_count": amount_count,
            "total_yuan": round(amount_total, 2) if amount_count else None,
            "avg_yuan": round(amount_total / amount_count, 2) if amount_count else None,
        },
        "structured": structured,
        "detail_url": detail_url,
        "generated_at": datetime.now(TZ).isoformat(),
    }


def build_feishu_card(brief: dict[str, Any]) -> dict[str, Any]:
    """将简报转为飞书 interactive card。"""
    days = brief.get("days", 1)
    is_weekend_daily = bool(brief.get("is_weekend_daily"))
    date_label = brief.get("date", "")
    if days > 1:
        date_label = f"{brief.get('date')} ~ {brief.get('date_end')}"
    stats_interval = (
        brief.get("window_label")
        if days == 1 or is_weekend_daily
        else date_label
    )

    count = brief.get("bim_count", 0)
    by_site = brief.get("by_site") or []
    amount_stats = brief.get("amount_stats") or {}
    detail_url = brief.get("detail_url") or f"{get_settings().bim_ui_url.rstrip('/')}/insights"

    week_new = brief.get("week_new_count")
    month_new = brief.get("month_new_count")
    period_lines: list[str] = []
    if week_new is not None:
        week_label = f"{brief.get('week_range_start')} ~ {brief.get('week_range_end')}"
        period_lines.append(f"**本周新增**　{week_new} 条（{week_label}）")
    if month_new is not None:
        month_label = f"{brief.get('month_range_start')} ~ {brief.get('month_range_end')}"
        period_lines.append(f"**近一月新增**　{month_new} 条（{month_label}）")
    period_block = ("\n".join(period_lines) + "\n\n") if period_lines else ""
    site_lines = []
    for row in by_site[:8]:
        name = row.get("name") or row.get("id") or "未知"
        pct = row.get("pct")
        pct_text = f"（{pct}%）" if pct is not None else ""
        site_lines.append(f"- {name}：**{row.get('count', 0)}** 条{pct_text}")

    top_lines = [
        _format_top_item_block(idx, item, detail_url)
        for idx, item in enumerate(brief.get("top_items") or [], start=1)
    ]

    amount_lines = []
    if amount_stats.get("disclosed_count"):
        total_wan = (amount_stats.get("total_yuan") or 0) / 1e4
        avg_wan = (amount_stats.get("avg_yuan") or 0) / 1e4
        amount_lines.append(
            f"已披露预算 **{amount_stats['disclosed_count']}** 条，"
            f"合计约 **{total_wan:.1f}** 万元，均约 **{avg_wan:.1f}** 万元"
        )
    else:
        amount_lines.append("预算金额：暂无足够结构化数据")

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**商机相关标讯**　{count} 条\n\n"
                    f"{period_block}"
                    f"**统计区间**　{stats_interval}"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**按站点分布**\n\n" + ("\n".join(site_lines) if site_lines else "暂无数据"),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**预算汇总**\n\n" + amount_lines[0],
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Top 标讯**\n\n" + ("\n\n---\n\n".join(top_lines) if top_lines else "暂无商机标讯"),
            },
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看商机洞察"},
                    "type": "primary",
                    "url": detail_url,
                }
            ],
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "<font color='grey'>仅院内环境可打开</font>",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>{_FEISHU_CARD_FOOTER}</font>",
            },
        },
    ]

    return {
        "header": build_feishu_card_header(
            _brief_report_title(days, date_label, is_weekend_daily=is_weekend_daily),
            template="blue",
        ),
        "elements": elements,
    }


def build_bim_weekly_feishu_card(brief: dict[str, Any]) -> dict[str, Any]:
    """将 BIM 周报简报转为飞书 interactive card（本周新增 + 近一月统计）。"""
    card = build_feishu_card(brief)
    date_label = f"{brief.get('date')} ~ {brief.get('date_end')}"
    days = brief.get("days", 7)
    title = f"商机招标周报 {date_label}"

    # 周报主统计区：突出本周新增与近一月分析
    elements = card.get("elements") or []
    if elements and elements[0].get("tag") == "div":
        count = brief.get("bim_count", 0)
        week_new = brief.get("week_new_count")
        month_new = brief.get("month_new_count")
        week_label = f"{brief.get('week_range_start')} ~ {brief.get('week_range_end')}"
        month_label = f"{brief.get('month_range_start')} ~ {brief.get('month_range_end')}"
        stat_lines = []
        if week_new is not None:
            stat_lines.append(f"**本周新增**　{week_new} 条（{week_label}）")
        if month_new is not None:
            stat_lines.append(f"**近一月新增**　{month_new} 条（{month_label}）")
        if days >= 28:
            stat_lines.append(f"**统计窗口**　近 {days} 日共 **{count}** 条商机标讯")
        else:
            stat_lines.append(f"**本周标讯**　{count} 条（{date_label}）")
        elements[0]["text"]["content"] = "\n\n".join(stat_lines)

    card["header"] = build_feishu_card_header(title, template="blue")
    return card


def _resolve_bim_brief_user_ids() -> list[str]:
    from src.core.chat_scheduled_tasks import resolve_env_feishu_receive_user_ids

    return resolve_env_feishu_receive_user_ids()


def _push_bim_feishu_card(
    card: dict[str, Any],
    *,
    channel: FeishuChannel = "auto",
    dry_run: bool = False,
    webhook_client: Optional[FeishuWebhookClient] = None,
    user_ids: Optional[list[str]] = None,
    webhooks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """推送 BIM 简报卡片：可同时推送到群 Webhook 与人员列表。"""
    from src.core.feishu_push import push_feishu_interactive_card_multi

    webhook_items = list(webhooks or [])
    if not webhook_items and webhook_client and webhook_client.configured:
        webhook_items = [{"url": webhook_client.webhook_url, "secret": webhook_client.secret}]
    recipients = [u for u in (user_ids or []) if u and str(u).strip()]

    if dry_run or (not webhook_items and not recipients):
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "reason": "no_push_channels",
            "payload": {"msg_type": "interactive", "card": card},
        }

    if webhook_items or recipients:
        return push_feishu_interactive_card_multi(
            card,
            webhooks=webhook_items,
            user_ids=recipients,
            dry_run=dry_run,
        )

    return send_feishu_interactive_card(
        card,
        channel=channel,
        dry_run=dry_run,
        webhook_client=webhook_client,
    )


def push_bim_weekly_brief(
    end_date: DateInput = None,
    *,
    days: int = 7,
    top_n: int = 10,
    dry_run: bool = False,
    channel: FeishuChannel = "auto",
    webhook_client: Optional[FeishuWebhookClient] = None,
    user_ids: Optional[list[str]] = None,
    webhooks: Optional[list[dict[str, Any]]] = None,
    last_week: bool = False,
) -> dict[str, Any]:
    """生成并推送 BIM 招标周报（本自然周周一至周五、近 N 日或上一自然周）。"""
    settings = get_settings()
    if not settings.bim_weekly_brief_enabled:
        logger.info("BIM 周报已禁用（BIM_WEEKLY_BRIEF_ENABLED=false），跳过")
        return {"ok": True, "skipped": True, "reason": "bim_weekly_brief_disabled"}

    if last_week:
        start, end = last_calendar_week_range(end_date)
        days = (end - start).days + 1
    elif days != 7:
        end = _parse_target_date(end_date)
        days = max(1, min(days, 31))
        start = end - timedelta(days=days - 1)
    else:
        start, end = weekly_report_range(end_date)
        days = (end - start).days + 1
    brief = build_bim_daily_brief(start, days=days, top_n=top_n)
    brief.update(compute_period_new_counts(end))
    card = build_bim_weekly_feishu_card(brief)

    from src.core.chat_scheduled_tasks import resolve_env_feishu_webhooks
    from src.core.feishu_push import is_feishu_push_configured

    resolved_users = user_ids if user_ids is not None else _resolve_bim_brief_user_ids()
    resolved_webhooks = list(webhooks or [])
    if not resolved_webhooks and webhook_client and webhook_client.configured:
        resolved_webhooks = [{"url": webhook_client.webhook_url, "secret": webhook_client.secret}]
    if not resolved_webhooks and not resolved_users:
        resolved_webhooks = [
            {k: v for k, v in item.items() if k != "source"}
            for item in resolve_env_feishu_webhooks()
        ]
    configured = bool(resolved_users) or bool(resolved_webhooks) or is_feishu_push_configured(channel)
    if dry_run or not configured:
        logger.info(
            "BIM 周报 %s ~ %s dry-run / 飞书未配置，共 %d 条",
            brief.get("date"),
            brief.get("date_end"),
            brief.get("bim_count", 0),
        )
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "brief": brief,
            "payload": {"msg_type": "interactive", "card": card},
        }

    result = _push_bim_feishu_card(
        card,
        channel=channel,
        dry_run=False,
        webhook_client=webhook_client,
        user_ids=resolved_users,
        webhooks=resolved_webhooks,
    )
    return {**result, "brief": brief}


def push_bim_daily_brief(
    target_date: DateInput = None,
    *,
    days: int = 1,
    top_n: int = 10,
    dry_run: bool = False,
    channel: FeishuChannel = "auto",
    webhook_client: Optional[FeishuWebhookClient] = None,
    user_ids: Optional[list[str]] = None,
    webhooks: Optional[list[dict[str, Any]]] = None,
    auto_window: bool = True,
) -> dict[str, Any]:
    """生成并推送 BIM 日报；无 webhook/IM 或 dry_run 时仅返回载荷。

    auto_window=True 且未指定 target_date、days=1 时：
    - 周一：统计上周末（周六+周日）
    - 其他：统计昨日
    """
    settings = get_settings()
    if not settings.bim_brief_enabled:
        logger.info("BIM 简报已禁用（BIM_BRIEF_ENABLED=false），跳过")
        return {"ok": True, "skipped": True, "reason": "bim_brief_disabled"}

    is_weekend_daily = False
    effective_days = days
    effective_target = target_date
    if auto_window and target_date is None and days == 1:
        effective_target, effective_days, is_weekend_daily = resolve_daily_report_window()

    brief = build_bim_daily_brief(
        effective_target,
        days=effective_days,
        top_n=top_n,
        is_weekend_daily=is_weekend_daily,
    )
    card = build_feishu_card(brief)

    from src.core.chat_scheduled_tasks import resolve_env_feishu_webhooks
    from src.core.feishu_push import is_feishu_push_configured

    resolved_users = user_ids if user_ids is not None else _resolve_bim_brief_user_ids()
    resolved_webhooks = list(webhooks or [])
    if not resolved_webhooks and webhook_client and webhook_client.configured:
        resolved_webhooks = [{"url": webhook_client.webhook_url, "secret": webhook_client.secret}]
    if not resolved_webhooks and not resolved_users:
        resolved_webhooks = [
            {k: v for k, v in item.items() if k != "source"}
            for item in resolve_env_feishu_webhooks()
        ]
    configured = bool(resolved_users) or bool(resolved_webhooks) or is_feishu_push_configured(channel)
    if dry_run or not configured:
        logger.info(
            "BIM 简报 %s dry-run / 飞书未配置，共 %d 条",
            brief.get("date"),
            brief.get("bim_count", 0),
        )
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "brief": brief,
            "payload": {"msg_type": "interactive", "card": card},
        }

    result = _push_bim_feishu_card(
        card,
        channel=channel,
        dry_run=False,
        webhook_client=webhook_client,
        user_ids=resolved_users,
        webhooks=resolved_webhooks,
    )
    return {**result, "brief": brief}


def generate_bim_daily_llm_analysis(brief: dict[str, Any]) -> Optional[dict[str, Any]]:
    """对近一月 BIM 简报做 LLM 综合分析（趋势、重点标讯、关键词等）。"""
    from src.core.llm_chat import LLMChatClient

    client = LLMChatClient()
    if not client.available:
        return None

    top_items = brief.get("top_items") or []
    payload = {
        "date": brief.get("date"),
        "bim_count": brief.get("bim_count", 0),
        "by_site": brief.get("by_site", [])[:8],
        "amount_stats": brief.get("amount_stats") or {},
        "structured": brief.get("structured") or {},
        "sample_items": [
            {
                "title": item.get("title"),
                "site": item.get("source_site_name") or item.get("source_site_id"),
                "budget": item.get("budget_amount"),
                "tags": item.get("bim_tags") or [],
                "summary": (item.get("summary") or "")[:200],
            }
            for item in top_items[:12]
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 BIM 招标情报分析师。根据近一月 BIM 标讯统计数据与样本，"
                "输出 JSON：summary(100字内总述), trends(数组,2-4条趋势观察), "
                "hot_keywords(数组,5-8个热词), key_projects(数组,3-5条重点标段简述), "
                "category_insights(数组,1-3条分类/类型观察), "
                "price_insights(数组,1-3条预算/价格观察), "
                "recommendations(数组,1-2条跟进建议)。"
                "只输出合法 JSON，不要编造不存在的信息；无数据时如实说明。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    content, err = client.chat(messages, temperature=0.2, timeout=90)
    if err or not content:
        logger.warning("BIM 每日分析 LLM 失败: %s", err)
        from src.core.llm_alert_service import maybe_alert_on_llm_error

        maybe_alert_on_llm_error(err or "LLM 返回空内容", source="bim_daily_llm_analysis")
        return None
    text = content.strip()
    if "```" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("BIM 每日分析 LLM 返回非 JSON")
        return {"summary": content[:600]}
    return {
        "summary": data.get("summary") or "",
        "trends": data.get("trends") or [],
        "hot_keywords": data.get("hot_keywords") or [],
        "key_projects": data.get("key_projects") or [],
        "category_insights": data.get("category_insights") or [],
        "price_insights": data.get("price_insights") or [],
        "recommendations": data.get("recommendations") or [],
    }


def _format_analysis_lines(items: list[Any], *, prefix: str = "- ") -> str:
    lines = []
    for item in items:
        text = str(item).strip()
        if text:
            lines.append(f"{prefix}{text}")
    return "\n".join(lines)


def build_bim_analysis_feishu_card(
    brief: dict[str, Any],
    analysis: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """将简报 + LLM 综合分析转为飞书 interactive card。"""
    card = build_feishu_card(brief)
    analysis_label = brief.get("window_label") or _analysis_card_date_label(brief)
    if not analysis:
        title = card.get("header", {}).get("title", {})
        if isinstance(title, dict):
            title["content"] = format_feishu_card_title(
                f"商机近一月综合分析 {analysis_label}"
            )
        return card

    analysis_blocks: list[str] = []
    if analysis.get("summary"):
        analysis_blocks.append(f"**综合摘要**\n{analysis['summary']}")
    if analysis.get("trends"):
        analysis_blocks.append("**趋势观察**\n" + _format_analysis_lines(analysis["trends"]))
    if analysis.get("hot_keywords"):
        kw = "、".join(str(k) for k in analysis["hot_keywords"][:8])
        analysis_blocks.append(f"**热词**：{kw}")
    if analysis.get("key_projects"):
        analysis_blocks.append("**重点标讯**\n" + _format_analysis_lines(analysis["key_projects"]))
    if analysis.get("category_insights"):
        analysis_blocks.append("**分类洞察**\n" + _format_analysis_lines(analysis["category_insights"]))
    if analysis.get("price_insights"):
        analysis_blocks.append("**预算观察**\n" + _format_analysis_lines(analysis["price_insights"]))
    if analysis.get("recommendations"):
        analysis_blocks.append("**跟进建议**\n" + _format_analysis_lines(analysis["recommendations"]))

    if analysis_blocks:
        insert_idx = max(0, len(card.get("elements") or []) - 1)
        card.setdefault("elements", []).insert(
            insert_idx,
            {"tag": "hr"},
        )
        card["elements"].insert(
            insert_idx + 1,
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n\n".join(analysis_blocks)},
            },
        )

    title = card.get("header", {}).get("title", {})
    if isinstance(title, dict):
        title["content"] = format_feishu_card_title(
            f"商机近一月综合分析 {analysis_label}"
        )
    return card


def _analysis_card_date_label(brief: dict[str, Any]) -> str:
    days = brief.get("days", 1)
    if days > 1:
        return f"{brief.get('date', '')} ~ {brief.get('date_end', '')}"
    return str(brief.get("date", ""))


def run_bim_daily_analysis(
    target_date: DateInput = None,
    *,
    top_n: int = 10,
    dry_run: bool = False,
    skip_llm: bool = False,
    channel: FeishuChannel = "auto",
    webhook_client: Optional[FeishuWebhookClient] = None,
    user_ids: Optional[list[str]] = None,
    webhooks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """查询近一月 BIM 标讯，LLM 综合分析并可选推送飞书。"""
    settings = get_settings()
    if not settings.bim_brief_enabled:
        logger.info("BIM 简报已禁用（BIM_BRIEF_ENABLED=false），跳过")
        return {"ok": True, "skipped": True, "reason": "bim_brief_disabled"}

    target = _parse_target_date(target_date)
    analysis_start = target - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
    brief = build_bim_daily_brief(
        analysis_start, days=ANALYSIS_WINDOW_DAYS, top_n=top_n
    )
    analysis: Optional[dict[str, Any]] = None
    if dry_run or skip_llm:
        analysis = {"summary": "[dry-run] 未调用 LLM 综合分析"}
    else:
        analysis = generate_bim_daily_llm_analysis(brief)

    card = build_bim_analysis_feishu_card(brief, analysis)
    payload = {"msg_type": "interactive", "card": card}

    from src.core.chat_scheduled_tasks import resolve_env_feishu_webhooks
    from src.core.feishu_push import is_feishu_push_configured

    resolved_users = user_ids if user_ids is not None else _resolve_bim_brief_user_ids()
    resolved_webhooks = list(webhooks or [])
    if not resolved_webhooks and webhook_client and webhook_client.configured:
        resolved_webhooks = [{"url": webhook_client.webhook_url, "secret": webhook_client.secret}]
    if not resolved_webhooks and not resolved_users:
        resolved_webhooks = [
            {k: v for k, v in item.items() if k != "source"}
            for item in resolve_env_feishu_webhooks()
        ]
    configured = bool(resolved_users) or bool(resolved_webhooks) or is_feishu_push_configured(channel)
    if dry_run or not configured:
        logger.info(
            "BIM 近一月分析 %s ~ %s dry-run / 飞书未配置，共 %d 条",
            brief.get("date"),
            brief.get("date_end"),
            brief.get("bim_count", 0),
        )
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "brief": brief,
            "analysis": analysis,
            "payload": payload,
        }

    result = _push_bim_feishu_card(
        card,
        channel=channel,
        dry_run=False,
        webhook_client=webhook_client,
        user_ids=resolved_users,
        webhooks=resolved_webhooks,
    )
    return {**result, "brief": brief, "analysis": analysis, "payload": payload}
