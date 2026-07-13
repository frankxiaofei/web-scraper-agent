"""Per-site interval 调度资格判定（与 Scheduler 一致）。"""

from __future__ import annotations

from typing import Any

from src.core.site_sync import _generic_has_crawl_rules


def site_eligible_for_schedule(site: dict[str, Any]) -> bool:
    adapter = site.get("adapter", "")
    if adapter != "generic":
        return True
    return _generic_has_crawl_rules(site)


def job_override_enabled(
    site_id: str,
    job_overrides: dict[str, dict[str, Any]] | None,
) -> bool:
    if not job_overrides:
        return True
    return job_overrides.get(site_id, {}).get("enabled", True)


def filter_schedule_eligible_sites(
    sites: list[dict[str, Any]],
    *,
    job_overrides: dict[str, dict[str, Any]] | None = None,
    require_enabled: bool = True,
) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for site in sites:
        if require_enabled and not site.get("enabled"):
            continue
        if not site_eligible_for_schedule(site):
            continue
        site_id = site.get("id", "")
        if not job_override_enabled(site_id, job_overrides):
            continue
        eligible.append(site)
    eligible.sort(key=lambda s: s.get("id", ""))
    return eligible
