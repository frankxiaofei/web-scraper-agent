"""Enabled site counting for billing quotas."""

from __future__ import annotations

from src.core.site_sync import load_sites_config


def count_enabled_sites() -> int:
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults") or {}
    count = 0
    for raw in config.get("sites") or []:
        site = {**crawl_defaults, **raw}
        if site.get("id") and site.get("enabled"):
            count += 1
    return count
