#!/usr/bin/env python3
"""批量探测全国招标/政采 BIM 站点可达性与列表抓取能力。

用法:
  # 探测全部 12 站（仅 fetch_list，不入库）
  python scripts/probe_national_bim_sites.py

  # 指定站点
  python scripts/probe_national_bim_sites.py zycg_national ggzy_national

  # 试跑 crawl_rules（generic 站）
  python scripts/probe_national_bim_sites.py --dry-run zycg_national

  # 单次抓取入库（需 MongoDB）
  python scripts/run_once.py zycg_national --max-items 5 --with-detail
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 飞书表格 12 站（与 config/sites.yaml 一致）
NATIONAL_BIM_SITE_IDS: tuple[str, ...] = (
    "zycg_national",
    "zcygov_national",
    "cebpubservice_national",
    "ggzy_national",
    "bidnews_national",
    "bidchance_national",
    "cecbid_national",
    "china_tender_national",
    "okcis_national",
    "bidcenter_national",
    "chinazbbid_national",
)


async def probe_adapter_list(site_id: str, max_items: int = 5) -> dict[str, Any]:
    from src.core.browser import BrowserPool
    from src.core.schedule_eligibility import site_eligible_for_schedule
    from src.core.scheduler import get_adapter_class, load_yaml
    from src.core.site_sync import get_site_by_id

    site = get_site_by_id(site_id)
    if not site:
        return {"site_id": site_id, "ok": False, "error": "站点不存在"}

    adapter_name = site.get("adapter", "generic")
    if adapter_name == "generic" and not site_eligible_for_schedule(site):
        return {
            "site_id": site_id,
            "name": site.get("name"),
            "adapter": adapter_name,
            "enabled": site.get("enabled"),
            "ok": False,
            "error": "generic 适配器缺少 crawl_rules.list_page",
            "sample": [],
        }

    schedule_path = ROOT / "config" / "schedule.yaml"
    schedule_config = load_yaml(schedule_path) if schedule_path.exists() else {}
    scheduler_cfg = schedule_config.get("scheduler", {})
    schedule_defaults = {
        "default_min_delay_seconds": scheduler_cfg.get("default_min_delay_seconds", 0),
        "max_retries": scheduler_cfg.get("max_retries", 3),
    }

    pool = BrowserPool(headless=True)
    await pool.start()
    try:
        adapter_cls = get_adapter_class(adapter_name)
        adapter = adapter_cls(site, pool)
        adapter.set_schedule_defaults(schedule_defaults)
        notices = await adapter.fetch_list(max_items=max_items)
        sample = [
            {"title": n.title[:80], "url": n.url}
            for n in notices[:max_items]
        ]
        return {
            "site_id": site_id,
            "name": site.get("name"),
            "adapter": adapter_name,
            "enabled": site.get("enabled"),
            "industry": site.get("industry"),
            "ok": len(notices) > 0,
            "count": len(notices),
            "sample": sample,
        }
    except Exception as exc:
        return {
            "site_id": site_id,
            "name": site.get("name"),
            "adapter": adapter_name,
            "ok": False,
            "error": str(exc),
            "sample": [],
        }
    finally:
        await pool.stop()


async def dry_run_rule(site_id: str, max_items: int = 5) -> dict[str, Any]:
    from src.core.crawl_rule_dry_run import dry_run_crawl_rule

    result = await dry_run_crawl_rule(site_id, max_items=max_items, max_pages=1)
    return {"site_id": site_id, "mode": "dry_run", **result}


async def main() -> None:
    parser = argparse.ArgumentParser(description="探测全国 BIM 招标站点")
    parser.add_argument(
        "site_ids",
        nargs="*",
        help="站点 ID，默认探测全部 12 站",
    )
    parser.add_argument("--max-items", type=int, default=5, help="每站最多抓取条数")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="对已有 crawl_rules 的站点试跑规则（不写库）",
    )
    args = parser.parse_args()

    site_ids = args.site_ids or list(NATIONAL_BIM_SITE_IDS)
    results: list[dict[str, Any]] = []

    for site_id in site_ids:
        print(f"探测 {site_id} ...", flush=True)
        if args.dry_run:
            results.append(await dry_run_rule(site_id, max_items=args.max_items))
        else:
            results.append(await probe_adapter_list(site_id, max_items=args.max_items))

    ok_count = sum(1 for r in results if r.get("ok") or r.get("success"))
    print(json.dumps({"summary": f"{ok_count}/{len(results)} 成功", "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
