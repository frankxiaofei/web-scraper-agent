"""爬取数据统计报表：按站点、类型、时间维度聚合。"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from src.core.site_sync import load_sites_config
from src.core.timezone_utils import app_today, to_app_tz
from src.web.data_service import NoticeDataService, _format_dt, _parse_dt, get_data_service

_OVERVIEW_CACHE_TTL_SECONDS = 60.0


def _load_site_metadata() -> tuple[dict[str, str], dict[str, str]]:
    """从 sites.yaml 读取平台名与公司名（company_name 优先，否则 parent）。"""
    names: dict[str, str] = {}
    companies: dict[str, str] = {}
    for site in load_sites_config().get("sites", []):
        sid = site.get("id")
        if not sid:
            continue
        if site.get("name"):
            names[str(sid)] = str(site["name"])
        company = site.get("company_name") or site.get("parent")
        if company:
            companies[str(sid)] = str(company).strip()
    return names, companies


def _doc_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    for key in ("scraped_at", "crawled_at", "created_at", "publish_date"):
        dt = _parse_dt(doc.get(key))
        if dt is not None:
            return dt
    return None


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _doc_source_name(doc: dict[str, Any]) -> str:
    return doc.get("source_site_name") or _doc_source_id(doc) or "未知站点"


def _doc_category(doc: dict[str, Any]) -> str:
    cat = doc.get("category")
    if cat is None or str(cat).strip() == "":
        return "未分类"
    return str(cat).strip()


def _with_pct(items: list[dict[str, Any]], *, total: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in items:
        count = int(row["count"])
        pct = round(count / total * 100, 1) if total else 0.0
        result.append({**row, "count": count, "pct": pct})
    result.sort(key=lambda r: (-r["count"], str(r.get("name") or r.get("id") or "")))
    return result


def _fill_daily(counts: dict[str, int], days: int = 30) -> list[dict[str, Any]]:
    today = app_today()
    series: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        key = d.strftime("%Y-%m-%d")
        series.append({"key": key, "label": d.strftime("%m-%d"), "count": counts.get(key, 0)})
    return series


def _fill_weekly(counts: dict[str, int], weeks: int = 12) -> list[dict[str, Any]]:
    today = app_today()
    # 以周一为一周起点
    start_of_week = today - timedelta(days=today.weekday())
    series: list[dict[str, Any]] = []
    for i in range(weeks - 1, -1, -1):
        week_start = start_of_week - timedelta(weeks=i)
        iso_year, iso_week, _ = week_start.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        week_end = week_start + timedelta(days=6)
        label = f"{week_start.strftime('%m/%d')}–{week_end.strftime('%m/%d')}"
        series.append({"key": key, "label": label, "count": counts.get(key, 0)})
    return series


def _fill_monthly(counts: dict[str, int], months: int = 12) -> list[dict[str, Any]]:
    today = app_today()
    series: list[dict[str, Any]] = []
    year, month = today.year, today.month
    for i in range(months - 1, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y:04d}-{m:02d}"
        series.append({"key": key, "label": f"{y}年{m}月", "count": counts.get(key, 0)})
    return series


class StatsService:
    """公告数据统计聚合。"""

    def __init__(self, data_service: Optional[NoticeDataService] = None) -> None:
        self._data = data_service or get_data_service()
        self._overview_cache: Optional[tuple[float, dict[str, Any]]] = None

    def get_overview(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._overview_cache is not None:
            cached_at, cached = self._overview_cache
            if now - cached_at < _OVERVIEW_CACHE_TTL_SECONDS:
                return cached
        if self._data.data_source == "mongodb":
            result = self._overview_from_mongo()
        else:
            result = self._overview_from_jsonl()
        self._overview_cache = (now, result)
        return result

    def _empty_overview(self) -> dict[str, Any]:
        return {
            "total": 0,
            "site_count": 0,
            "category_count": 0,
            "last_updated": None,
            "last_updated_fmt": "—",
            "by_site": [],
            "by_category": [],
            "by_time": {
                "daily": _fill_daily({}),
                "weekly": _fill_weekly({}),
                "monthly": _fill_monthly({}),
            },
            "has_data": False,
        }

    def _build_overview(
        self,
        *,
        total: int,
        site_counter: Counter[str],
        site_names: dict[str, str],
        category_counter: Counter[str],
        daily_counts: dict[str, int],
        weekly_counts: dict[str, int],
        monthly_counts: dict[str, int],
        last_updated: Optional[datetime],
    ) -> dict[str, Any]:
        if total == 0:
            return self._empty_overview()

        yaml_names, yaml_companies = _load_site_metadata()
        by_site = _with_pct(
            [
                {
                    "id": sid,
                    "company_name": yaml_companies.get(sid) or "",
                    "name": site_names.get(sid) or yaml_names.get(sid) or sid or "未知站点",
                    "count": cnt,
                }
                for sid, cnt in site_counter.items()
            ],
            total=total,
        )
        by_category = _with_pct(
            [{"name": name, "count": cnt} for name, cnt in category_counter.items()],
            total=total,
        )
        return {
            "total": total,
            "site_count": len(site_counter),
            "category_count": len(category_counter),
            "last_updated": last_updated.isoformat() if last_updated else None,
            "last_updated_fmt": _format_dt(last_updated) if last_updated else "—",
            "by_site": by_site,
            "by_category": by_category,
            "by_time": {
                "daily": _fill_daily(daily_counts),
                "weekly": _fill_weekly(weekly_counts),
                "monthly": _fill_monthly(monthly_counts),
            },
            "has_data": True,
        }

    def _overview_from_jsonl(self) -> dict[str, Any]:
        docs = [
            doc
            for doc in self._data._load_jsonl()  # noqa: SLF001
            if self._data._in_mvp_scope(doc)  # noqa: SLF001
        ]
        if not docs:
            return self._empty_overview()

        site_counter: Counter[str] = Counter()
        site_names: dict[str, str] = {}
        category_counter: Counter[str] = Counter()
        daily_counts: dict[str, int] = defaultdict(int)
        weekly_counts: dict[str, int] = defaultdict(int)
        monthly_counts: dict[str, int] = defaultdict(int)
        last_updated: Optional[datetime] = None

        for doc in docs:
            sid = _doc_source_id(doc)
            site_counter[sid] += 1
            site_names[sid] = _doc_source_name(doc)
            category_counter[_doc_category(doc)] += 1

            ts = _doc_timestamp(doc)
            if ts is not None:
                local_ts = to_app_tz(ts)
                if last_updated is None or local_ts > last_updated:
                    last_updated = local_ts
                daily_counts[local_ts.strftime("%Y-%m-%d")] += 1
                iso_year, iso_week, _ = local_ts.date().isocalendar()
                weekly_counts[f"{iso_year}-W{iso_week:02d}"] += 1
                monthly_counts[local_ts.strftime("%Y-%m")] += 1

        return self._build_overview(
            total=len(docs),
            site_counter=site_counter,
            site_names=site_names,
            category_counter=category_counter,
            daily_counts=dict(daily_counts),
            weekly_counts=dict(weekly_counts),
            monthly_counts=dict(monthly_counts),
            last_updated=last_updated,
        )

    def _overview_from_mongo(self) -> dict[str, Any]:
        coll = self._data._mongo_coll  # noqa: SLF001
        mvp_ids = self._data._enabled_site_ids()  # noqa: SLF001
        if coll is None or not mvp_ids:
            return self._empty_overview()

        base_match = self._data._mvp_source_filter_mongo(mvp_ids)  # noqa: SLF001
        source_id_expr = {
            "$ifNull": ["$source_site_id", {"$ifNull": ["$source", "$site_id"]}]
        }
        ts_expr = {
            "$ifNull": [
                "$scraped_at",
                {"$ifNull": ["$crawled_at", "$created_at"]},
            ]
        }

        total = coll.count_documents(base_match)

        site_rows = list(
            coll.aggregate(
                [
                    {"$match": base_match},
                    {
                        "$group": {
                            "_id": source_id_expr,
                            "count": {"$sum": 1},
                            "name": {"$first": "$source_site_name"},
                        }
                    },
                ]
            )
        )
        category_rows = list(
            coll.aggregate(
                [
                    {"$match": base_match},
                    {
                        "$group": {
                            "_id": {
                                "$cond": [
                                    {
                                        "$or": [
                                            {"$eq": ["$category", None]},
                                            {"$eq": ["$category", ""]},
                                        ]
                                    },
                                    "未分类",
                                    "$category",
                                ]
                            },
                            "count": {"$sum": 1},
                        }
                    },
                ]
            )
        )

        last_doc = coll.find_one(
            base_match,
            sort=[("scraped_at", -1), ("crawled_at", -1), ("created_at", -1)],
        )
        last_updated = _doc_timestamp(last_doc) if last_doc else None

        # 时间趋势：拉取时间戳后在 Python 侧分桶（兼容字符串/日期混存）
        daily_counts: dict[str, int] = defaultdict(int)
        weekly_counts: dict[str, int] = defaultdict(int)
        monthly_counts: dict[str, int] = defaultdict(int)
        cursor = coll.find(base_match, {"scraped_at": 1, "crawled_at": 1, "created_at": 1})
        for doc in cursor:
            ts = _doc_timestamp(doc)
            if ts is None:
                continue
            local_ts = to_app_tz(ts)
            daily_counts[local_ts.strftime("%Y-%m-%d")] += 1
            iso_year, iso_week, _ = local_ts.date().isocalendar()
            weekly_counts[f"{iso_year}-W{iso_week:02d}"] += 1
            monthly_counts[local_ts.strftime("%Y-%m")] += 1

        site_counter = Counter({str(r["_id"]): int(r["count"]) for r in site_rows if r.get("_id")})
        site_names = {
            str(r["_id"]): str(r.get("name") or r["_id"])
            for r in site_rows
            if r.get("_id")
        }
        category_counter = Counter(
            {str(r["_id"]): int(r["count"]) for r in category_rows if r.get("_id") is not None}
        )

        return self._build_overview(
            total=total,
            site_counter=site_counter,
            site_names=site_names,
            category_counter=category_counter,
            daily_counts=dict(daily_counts),
            weekly_counts=dict(weekly_counts),
            monthly_counts=dict(monthly_counts),
            last_updated=last_updated,
        )


_service: Optional[StatsService] = None


def get_stats_service() -> StatsService:
    global _service
    if _service is None:
        _service = StatsService()
    return _service
