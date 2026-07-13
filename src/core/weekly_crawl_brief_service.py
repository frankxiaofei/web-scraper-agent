"""采集周报：近 7 日公告与爬取 run 汇总，飞书推送。"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Union

from src.core.agri_classifier import doc_is_agri_related
from src.core.config import get_settings
from src.core.crawl_run_store import get_crawl_run_store
from src.core.feishu_push import FeishuChannel, send_feishu_interactive_card
from src.core.feishu_webhook import FeishuWebhookClient, build_feishu_card_header
from src.core.timezone_utils import APP_TZ, coerce_dt, to_app_tz
from src.web.data_service import NoticeDataService, doc_crawl_timestamp, get_data_service
from src.web.stats_service import _load_site_metadata, _with_pct

logger = logging.getLogger(__name__)

TZ = APP_TZ
DateInput = Union[date, datetime, str, None]


def _parse_end_date(value: DateInput) -> date:
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


def _week_window(end: date, days: int) -> tuple[date, date, datetime, datetime]:
    days = max(1, days)
    start_date = end - timedelta(days=days - 1)
    window_start = datetime.combine(start_date, time.min, tzinfo=TZ)
    window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=TZ)
    return start_date, end, window_start, window_end


def _ts_in_range(ts: Optional[datetime], start: datetime, end: datetime) -> bool:
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ)
    else:
        ts = ts.astimezone(TZ)
    return start <= ts < end


def _count_notices_in_window(
    data: NoticeDataService,
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int, list[dict[str, Any]]]:
    """按采集时间统计窗口内公告总数、数字农业相关数、按站点分布。"""
    yaml_names, yaml_companies = _load_site_metadata()
    site_counter: Counter[str] = Counter()
    site_names: dict[str, str] = {}
    total = 0
    agri_total = 0

    if data.data_source == "mongodb":
        coll = data._mongo_coll  # noqa: SLF001
        mvp_ids = data._enabled_site_ids()  # noqa: SLF001
        if coll is None or not mvp_ids:
            return 0, 0, []
        base_match = data._mvp_source_filter_mongo(mvp_ids)  # noqa: SLF001
        cursor = coll.find(
            base_match,
            {
                "scraped_at": 1,
                "crawled_at": 1,
                "created_at": 1,
                "is_agri_related": 1,
                "source_site_id": 1,
                "source": 1,
                "site_id": 1,
                "source_site_name": 1,
            },
        )
        for doc in cursor:
            ts = doc_crawl_timestamp(doc)
            if not _ts_in_range(ts, window_start, window_end):
                continue
            total += 1
            if doc_is_agri_related(doc):
                agri_total += 1
            sid = doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""
            site_counter[sid] += 1
            site_names[sid] = doc.get("source_site_name") or yaml_names.get(sid) or sid
    else:
        for doc in data._load_jsonl():  # noqa: SLF001
            if not data._in_mvp_scope(doc):  # noqa: SLF001
                continue
            ts = doc_crawl_timestamp(doc)
            if not _ts_in_range(ts, window_start, window_end):
                continue
            total += 1
            if doc_is_agri_related(doc):
                agri_total += 1
            sid = doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""
            site_counter[sid] += 1
            site_names[sid] = doc.get("source_site_name") or yaml_names.get(sid) or sid

    by_site = _with_pct(
        [
            {
                "id": sid,
                "name": site_names.get(sid) or yaml_names.get(sid) or sid or "未知站点",
                "company_name": yaml_companies.get(sid) or "",
                "count": cnt,
            }
            for sid, cnt in site_counter.most_common()
        ],
        total=total,
    )
    return total, agri_total, by_site


def _summarize_crawl_runs(window_start: datetime, window_end: datetime) -> dict[str, Any]:
    store = get_crawl_run_store()
    runs = store.list_runs(limit=500)
    in_window: list[dict[str, Any]] = []
    for run in runs:
        started = coerce_dt(run.get("started_at") or run.get("created_at"))
        if not _ts_in_range(started, window_start, window_end):
            continue
        in_window.append(run)

    status_counter = Counter(str(r.get("status") or "unknown") for r in in_window)
    failed_runs = [r for r in in_window if r.get("status") == "failed"]
    return {
        "total_runs": len(in_window),
        "success_runs": status_counter.get("success", 0),
        "failed_runs": status_counter.get("failed", 0),
        "running_runs": status_counter.get("running", 0),
        "notices_collected": sum(int(r.get("notices_count") or 0) for r in in_window),
        "new_notices": sum(int(r.get("new_count") or 0) for r in in_window),
        "failed_samples": [
            {
                "site_id": r.get("site_id"),
                "site_name": r.get("site_name") or r.get("site_id"),
                "error": (r.get("error") or "未知错误")[:120],
                "started_at": r.get("started_at"),
            }
            for r in failed_runs[:5]
        ],
    }


def build_weekly_crawl_brief(
    end_date: DateInput = None,
    *,
    days: int = 7,
    top_n: int = 8,
) -> dict[str, Any]:
    """生成近 N 日采集周报（含全量公告、数字农业、站点分布、爬取 run）。"""
    end = _parse_end_date(end_date)
    days = max(1, min(days, 31))
    start_date, end_date_val, window_start, window_end = _week_window(end, days)

    data = get_data_service()
    total, agri_by_crawl, by_site = _count_notices_in_window(data, window_start, window_end)
    run_stats = _summarize_crawl_runs(window_start, window_end)

    from src.web.agri_insights_service import get_agri_insights_service

    agri_summary = get_agri_insights_service().get_summary(days=days)
    db_total = None
    if data.data_source == "mongodb" and data._mongo_coll is not None:  # noqa: SLF001
        mvp_ids = data._enabled_site_ids()  # noqa: SLF001
        db_total = data._mongo_coll.count_documents(  # noqa: SLF001
            data._mvp_source_filter_mongo(mvp_ids)  # noqa: SLF001
        )

    settings = get_settings()
    return {
        "report_type": "weekly_crawl",
        "date_start": start_date.isoformat(),
        "date_end": end_date_val.isoformat(),
        "days": days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "total_notices": total,
        "agri_count_crawl": agri_by_crawl,
        "agri_count_publish": agri_summary.get("agri_count_in_window", 0),
        "db_total_notices": db_total,
        "by_site": by_site,
        "run_stats": run_stats,
        "agri_summary": agri_summary,
        "detail_url": f"{settings.public_base_url.rstrip('/')}/insights",
        "generated_at": datetime.now(TZ).isoformat(),
    }


def build_weekly_feishu_card(brief: dict[str, Any]) -> dict[str, Any]:
    """将采集周报转为飞书 interactive card。"""
    date_label = f"{brief.get('date_start')} ~ {brief.get('date_end')}"
    by_site = brief.get("by_site") or []
    run_stats = brief.get("run_stats") or {}
    agri_summary = brief.get("agri_summary") or {}
    hot_keywords = agri_summary.get("hot_keywords") or []
    hot_line = "、".join(f"{h.get('keyword')}({h.get('count')})" for h in hot_keywords[:5]) or "—"

    site_lines = []
    for row in by_site[:8]:
        name = row.get("name") or row.get("id") or "未知"
        pct = row.get("pct")
        pct_text = f" ({pct}%)" if pct is not None else ""
        site_lines.append(f"- {name}: **{row.get('count', 0)}** 条{pct_text}")

    failed_lines = []
    for item in run_stats.get("failed_samples") or []:
        site = item.get("site_name") or item.get("site_id") or "—"
        err = (item.get("error") or "").replace("\n", " ")
        failed_lines.append(f"- {site}: {err}")

    db_total = brief.get("db_total_notices")
    db_line = f"库内累计 **{db_total}** 条" if db_total is not None else "库内累计：—"

    top_block = ""
    latest = (agri_summary.get("latest") or []) if isinstance(agri_summary.get("latest"), list) else []
    if latest:
        lines = [f"{i}. {item.get('title', '')[:48]}" for i, item in enumerate(latest[:5], 1)]
        top_block = "**农业 Top 标讯**\n" + "\n".join(lines)

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**统计周期**：{date_label}\n"
                    f"**本周新采集**：**{brief.get('total_notices', 0)}** 条\n"
                    f"**数字农业**：采集 **{brief.get('agri_count_crawl', 0)}** 条"
                    f" / 发布窗口 **{brief.get('agri_count_publish', 0)}** 条\n"
                    f"**热词**：{hot_line}\n"
                    f"{db_line}\n"
                    f"**爬取任务**：共 **{run_stats.get('total_runs', 0)}** 次，"
                    f"成功 **{run_stats.get('success_runs', 0)}**，"
                    f"失败 **{run_stats.get('failed_runs', 0)}**；"
                    f"新增 **{run_stats.get('new_notices', 0)}** 条"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**按站点（本周采集）**\n"
                + ("\n".join(site_lines) if site_lines else "暂无数据"),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**失败任务**\n"
                + ("\n".join(failed_lines) if failed_lines else "本周无失败任务"),
            },
        },
    ]

    if top_block:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": top_block}},
            ]
        )

    detail_url = brief.get("detail_url") or "http://127.0.0.1:8090/stats"
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看采集统计"},
                    "type": "primary",
                    "url": detail_url,
                }
            ],
        }
    )

    return {
        "header": build_feishu_card_header(f"采集周报 {date_label}", template="wathet"),
        "elements": elements,
    }


def push_weekly_crawl_brief(
    end_date: DateInput = None,
    *,
    days: int = 7,
    top_n: int = 8,
    dry_run: bool = False,
    channel: FeishuChannel = "auto",
    webhook_client: Optional[FeishuWebhookClient] = None,
) -> dict[str, Any]:
    """生成并推送采集周报。"""
    brief = build_weekly_crawl_brief(end_date, days=days, top_n=top_n)
    card = build_weekly_feishu_card(brief)

    from src.core.feishu_push import is_feishu_push_configured

    if dry_run or not is_feishu_push_configured(channel):
        logger.info(
            "采集周报 %s ~ %s dry-run / 飞书未配置，新采集 %d 条",
            brief.get("date_start"),
            brief.get("date_end"),
            brief.get("total_notices", 0),
        )
        return {
            "ok": True,
            "skipped": True,
            "dry_run": dry_run,
            "brief": brief,
            "payload": {"msg_type": "interactive", "card": card},
        }

    result = send_feishu_interactive_card(
        card,
        channel=channel,
        dry_run=False,
        webhook_client=webhook_client,
    )
    return {**result, "brief": brief}
