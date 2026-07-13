#!/usr/bin/env python3
"""批量探测省级政采/公共资源与央企采购站点列表抓取能力。

用法:
  # 探测全部省级 + 央企（仅 fetch_list，不入库）
  python scripts/probe_provincial_soe_sites.py

  # 仅省级
  python scripts/probe_provincial_soe_sites.py --category provincial

  # 仅央企
  python scripts/probe_provincial_soe_sites.py --category enterprise

  # 指定站点
  python scripts/probe_provincial_soe_sites.py ccgp_江苏省 ggzy_浙江省 ecp_sgcc

  # 试跑 crawl_rules（generic 站）
  python scripts/probe_provincial_soe_sites.py --dry-run 中国铁道建筑集团有限公司_物资采购网
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


def _load_site_ids(category: str | None) -> list[str]:
    import yaml

    with open(ROOT / "config" / "sites.yaml", encoding="utf-8") as f:
        sites = yaml.safe_load(f).get("sites", [])

    if category == "provincial":
        cats = {"provincial_gov", "provincial_ggzy"}
        return [s["id"] for s in sites if s.get("category") in cats]
    if category == "enterprise":
        return [
            s["id"]
            for s in sites
            if s.get("category") == "enterprise" and s["id"] != "dlzb_power"
        ]
    if category == "national":
        return [s["id"] for s in sites if s.get("category") == "national"]
    # 默认：省级 + 央企
    return [
        s["id"]
        for s in sites
        if s.get("category") in ("provincial_gov", "provincial_ggzy", "enterprise")
        and s["id"] != "dlzb_power"
    ]


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
            "category": site.get("category"),
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
        sample = [{"title": n.title[:80], "url": n.url} for n in notices[:max_items]]
        return {
            "site_id": site_id,
            "name": site.get("name"),
            "category": site.get("category"),
            "adapter": adapter_name,
            "enabled": site.get("enabled"),
            "industry": site.get("industry"),
            "soe": site.get("soe"),
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
    parser = argparse.ArgumentParser(description="探测省级/央企招标站点")
    parser.add_argument("site_ids", nargs="*", help="站点 ID，默认探测全部省级+央企")
    parser.add_argument(
        "--category",
        choices=("provincial", "enterprise", "national"),
        help="按类别筛选",
    )
    parser.add_argument("--max-items", type=int, default=5, help="每站最多抓取条数")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="对已有 crawl_rules 的站点试跑规则（不写库）",
    )
    args = parser.parse_args()

    site_ids = args.site_ids or _load_site_ids(args.category)
    results: list[dict[str, Any]] = []

    for site_id in site_ids:
        print(f"探测 {site_id} ...", flush=True)
        if args.dry_run:
            results.append(await dry_run_rule(site_id, max_items=args.max_items))
        else:
            results.append(await probe_adapter_list(site_id, max_items=args.max_items))

    ok_count = sum(1 for r in results if r.get("ok") or r.get("success"))
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        cat = r.get("category") or "unknown"
        bucket = by_cat.setdefault(cat, {"ok": 0, "total": 0})
        bucket["total"] += 1
        if r.get("ok") or r.get("success"):
            bucket["ok"] += 1

    print(
        json.dumps(
            {
                "summary": f"{ok_count}/{len(results)} 成功",
                "by_category": by_cat,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
