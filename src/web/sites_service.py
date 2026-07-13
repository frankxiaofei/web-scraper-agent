"""站点配置清单聚合服务（sites.yaml / crawl_rules / schedule / script_cron）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.crawl_rules import get_rule_loader, load_crawl_rule
from src.core.script_cron import list_script_cron_jobs
from src.core.site_sync import load_sites_config
from src.core.sync_status import get_sync_status_store
from src.web.sync_service import _format_display_dt

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"
CRAWL_SCRIPTS_DIR = ROOT / "data" / "crawl_scripts"


def _load_schedule_data() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not SCHEDULE_PATH.is_file():
        return {}, {}
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    overrides: dict[str, dict[str, Any]] = {}
    for job in data.get("jobs", []):
        site_id = job.get("site_id")
        if site_id:
            overrides[site_id] = job
    return data.get("scheduler", {}), overrides


def _extract_site_id_from_args(args: Any) -> Optional[str]:
    if not isinstance(args, list):
        return None
    for i, arg in enumerate(args):
        text = str(arg).strip()
        if text in ("--site-id", "--site_id") and i + 1 < len(args):
            return str(args[i + 1]).strip() or None
    return None


def _build_script_cron_index() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for job in list_script_cron_jobs():
        site_id = _extract_site_id_from_args(job.get("args"))
        if not site_id:
            continue
        index.setdefault(site_id, []).append(job)
    return index


def _format_schedule_job(job: dict[str, Any]) -> str:
    trigger = (job.get("trigger") or "interval").strip().lower()
    if trigger == "cron" and job.get("cron"):
        return f"cron {job['cron']}"
    minutes = job.get("minutes") or job.get("interval_minutes")
    if minutes:
        return f"每 {int(minutes)} 分钟"
    return "interval"


def _format_crawl_method(site: dict[str, Any], *, has_rule: bool) -> str:
    adapter = (site.get("adapter") or "generic").strip()
    if has_rule:
        if adapter and adapter != "generic":
            return f"{adapter} + 规则"
        return "规则"
    return adapter or "generic"


def _has_crawl_rule(site: dict[str, Any]) -> bool:
    loader = get_rule_loader()
    if loader.path_for(site.get("id", "")).is_file():
        return True
    if isinstance(site.get("crawl_rules"), dict):
        return True
    rule = load_crawl_rule(site)
    return bool(rule and rule.list_page)


def _load_crawl_script_meta(site_id: str) -> Optional[dict[str, Any]]:
    path = CRAWL_SCRIPTS_DIR / f"{site_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _is_configured_site(
    site: dict[str, Any],
    *,
    has_rule: bool,
    schedule_job: Optional[dict[str, Any]],
    script_jobs: list[dict[str, Any]],
    crawl_script: Optional[dict[str, Any]],
) -> bool:
    if site.get("enabled"):
        return True
    if has_rule:
        return True
    if schedule_job:
        return True
    if script_jobs:
        return True
    if crawl_script:
        return True
    return False


def _build_schedule_hint(
    site: dict[str, Any],
    schedule_job: Optional[dict[str, Any]],
    script_jobs: list[dict[str, Any]],
    scheduler: dict[str, Any],
) -> str:
    parts: list[str] = []
    if schedule_job:
        label = _format_schedule_job(schedule_job)
        if not schedule_job.get("enabled", True):
            label += "（已禁用）"
        parts.append(label)
    elif site.get("enabled") and scheduler.get("per_site_schedule_enabled", True):
        stagger = int(scheduler.get("round_robin_interval_minutes", 10))
        parts.append(f"轮询 每 {stagger} 分钟/站")

    for job in script_jobs:
        cron = job.get("cron") or "—"
        name = job.get("name") or job.get("script") or "script_cron"
        enabled = "启用" if job.get("enabled", True) else "禁用"
        parts.append(f"{name}: {cron}（{enabled}）")

    return " · ".join(parts) if parts else "—"


class SitesService:
    def list_sites(self, *, include_all: bool = False) -> dict[str, Any]:
        config = load_sites_config()
        crawl_defaults = config.get("crawl_defaults", {})
        scheduler, schedule_jobs = _load_schedule_data()
        script_cron_index = _build_script_cron_index()
        store = get_sync_status_store()

        site_configs: list[dict[str, Any]] = []
        for raw in config.get("sites", []):
            site = {**crawl_defaults, **raw}
            if site.get("id"):
                site_configs.append(site)

        sync_rows = {
            row["site_id"]: row for row in store.list_all(site_configs)
        }

        sites: list[dict[str, Any]] = []
        for site in site_configs:
            site_id = site["id"]
            has_rule = _has_crawl_rule(site)
            schedule_job = schedule_jobs.get(site_id)
            script_jobs = script_cron_index.get(site_id, [])
            crawl_script = _load_crawl_script_meta(site_id)

            if not include_all and not _is_configured_site(
                site,
                has_rule=has_rule,
                schedule_job=schedule_job,
                script_jobs=script_jobs,
                crawl_script=crawl_script,
            ):
                continue

            sync_row = sync_rows.get(site_id, {})
            last_sync_at = sync_row.get("last_sync_at") or sync_row.get("last_success_at")

            sites.append(
                {
                    "site_id": site_id,
                    "site_name": site.get("name") or site_id,
                    "site_url": site.get("url") or "",
                    "enabled": bool(site.get("enabled")),
                    "crawl_method": _format_crawl_method(site, has_rule=has_rule),
                    "adapter": site.get("adapter") or "generic",
                    "has_rule": has_rule,
                    "schedule_hint": _build_schedule_hint(
                        site, schedule_job, script_jobs, scheduler
                    ),
                    "schedule_job_enabled": (
                        bool(schedule_job.get("enabled", True)) if schedule_job else None
                    ),
                    "last_sync_at": last_sync_at,
                    "last_sync_at_fmt": _format_display_dt(last_sync_at),
                    "has_crawl_script": crawl_script is not None,
                    "crawl_script_type": (crawl_script or {}).get("script_type"),
                    "script_cron_count": len(script_jobs),
                }
            )

        sites.sort(key=lambda s: (not s["enabled"], s["site_name"]))

        return {
            "summary": {
                "total": len(sites),
                "enabled": sum(1 for s in sites if s["enabled"]),
                "with_rules": sum(1 for s in sites if s["has_rule"]),
                "with_schedule": sum(1 for s in sites if s["schedule_hint"] != "—"),
            },
            "sites": sites,
        }

    def delete_site(self, site_id: str) -> dict[str, Any]:
        from src.core.site_config_write import delete_site

        return delete_site(site_id)


_service: Optional[SitesService] = None


def get_sites_service() -> SitesService:
    global _service
    if _service is None:
        _service = SitesService()
    return _service
