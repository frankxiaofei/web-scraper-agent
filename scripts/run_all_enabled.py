#!/usr/bin/env python3
"""串行抓取 sites.yaml 中所有 enabled 站点，汇总 TSV 与日志。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.browser import BrowserPool
from src.core.config import get_settings
from src.core.pipeline import Pipeline
from src.core.scheduler import get_adapter_class, load_yaml
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_sync import get_crawl_scope_label, load_enabled_sites
from src.db.mongo_repository import create_mongo_repository

SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"
DATA_DIR = ROOT / "data"

DETAIL_ADAPTERS = frozenset({"ccgp", "ccgp_provincial", "ggzy", "ggzy_provincial"})

TZ_CN = timezone(timedelta(hours=8))


class TeeWriter:
    """同时写入终端与日志文件。"""

    def __init__(self, stream: TextIO, log_file: TextIO) -> None:
        self._stream = stream
        self._log_file = log_file

    def write(self, data: str) -> int:
        self._stream.write(data)
        self._log_file.write(data)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        self._log_file.flush()


def should_fetch_detail(site: dict[str, Any]) -> bool:
    adapter = site.get("adapter", "")
    return bool(site.get("fetch_detail")) and adapter in DETAIL_ADAPTERS


def make_run_paths() -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    return (
        DATA_DIR / f"scrape_run_{stamp}.log",
        DATA_DIR / f"scrape_results_{stamp}.tsv",
    )


async def scrape_site(
    site: dict[str, Any],
    max_items: int,
    schedule_defaults: dict[str, Any],
    fetch_detail: bool,
    intelligent: bool = False,
    mongo_repo: Any = None,
) -> dict[str, Any]:
    site_id = site["id"]
    adapter_name = site.get("adapter", "generic")
    if not site_eligible_for_schedule(site):
        return {
            "site_id": site_id,
            "status": "skip",
            "scraped": 0,
            "new": 0,
            "seconds": 0,
            "error": "尚未配置适配器",
        }

    pool = BrowserPool(headless=True)
    pipeline = Pipeline(settings=get_settings())
    await pool.start()
    started = time.monotonic()

    try:
        adapter_cls = get_adapter_class(adapter_name)
        adapter = adapter_cls(site, pool)
        adapter.set_schedule_defaults(schedule_defaults)
        result = await adapter.run(
            max_items=max_items,
            fetch_detail=True if fetch_detail else None,
            intelligent=True if intelligent else None,
            mongo_repo=mongo_repo,
        )
        elapsed = int(time.monotonic() - started)

        if not result.success:
            return {
                "site_id": site_id,
                "status": "fail",
                "scraped": "?",
                "new": "?",
                "seconds": elapsed,
                "error": result.error or "unknown error",
            }

        processed = pipeline.process(result)
        pipeline.save(processed)

        print(f"\n=== {site['name']} ({site_id}) ===")
        print(
            f"抓取 {len(result.notices)} 条，新增 {len(processed.new)} 条，"
            f"正文补全 {len(processed.enriched)} 条"
        )
        display_notices = processed.all_for_storage or (
            result.notices if fetch_detail else []
        )
        for n in display_notices[:max_items]:
            date_str = n.publish_date.strftime("%Y-%m-%d") if n.publish_date else "-"
            print(f"  [{date_str}] {n.title[:60]}")
            print(f"           {n.url}")
            if n.content_text:
                preview = n.content_text[:80].replace("\n", " ")
                print(f"           正文: {preview}...")
            if n.purchaser:
                print(f"           采购人: {n.purchaser}")
            if n.budget:
                print(f"           预算: {n.budget}")
            if n.attachments:
                print(f"           附件: {len(n.attachments)} 个")

        return {
            "site_id": site_id,
            "status": "ok",
            "scraped": len(result.notices),
            "new": len(processed.new) + len(processed.enriched),
            "seconds": elapsed,
            "error": "",
        }
    except Exception as exc:
        elapsed = int(time.monotonic() - started)
        logging.getLogger("run_all_enabled").exception("站点 %s 异常", site_id)
        return {
            "site_id": site_id,
            "status": "fail",
            "scraped": "?",
            "new": "?",
            "seconds": elapsed,
            "error": str(exc),
        }
    finally:
        await pool.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(description="串行抓取所有 enabled 站点")
    parser.add_argument("--max-items", type=int, default=10, help="每站最多抓取条数")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="[已废弃] 等同于 --intelligent",
    )
    parser.add_argument(
        "--intelligent",
        action="store_true",
        help="启用智能爬取（覆盖各站点 enable_recursive 配置）",
    )
    args = parser.parse_args()

    try:
        enabled_sites = load_enabled_sites()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    if not enabled_sites:
        print(
            f"sites.yaml 中没有符合 crawl_scope 的 enabled 站点（当前: {get_crawl_scope_label()}）",
            file=sys.stderr,
        )
        sys.exit(1)

    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )

    log_path, tsv_path = make_run_paths()
    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    scheduler_cfg = schedule_config.get("scheduler", {})
    schedule_defaults = {
        "default_min_delay_seconds": scheduler_cfg.get("default_min_delay_seconds", 0),
        "max_retries": scheduler_cfg.get("max_retries", 3),
    }

    log_file = log_path.open("w", encoding="utf-8")
    started_at = datetime.now(TZ_CN)
    log_file.write(f"started_at={started_at.isoformat(timespec='seconds')}\n")
    log_file.flush()

    tee_out = TeeWriter(sys.stdout, log_file)
    tee_err = TeeWriter(sys.stderr, log_file)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = tee_out, tee_err  # type: ignore[assignment]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    rows: list[dict[str, Any]] = []
    total_started = time.monotonic()

    try:
        for site in enabled_sites:
            site_id = site["id"]
            fetch_detail = should_fetch_detail(site)
            site_intelligent = (
                bool(site.get("enable_recursive"))
                or args.intelligent
                or args.recursive
            )
            detail_flag = " --with-detail" if fetch_detail else ""
            intel_flag = " --intelligent" if site_intelligent else ""
            print(f"========== {site_id} ==========")
            print(
                f"# run_once {site_id} --max-items {args.max_items}{detail_flag}{intel_flag}",
                flush=True,
            )

            row = await scrape_site(
                site,
                max_items=args.max_items,
                schedule_defaults=schedule_defaults,
                fetch_detail=fetch_detail,
                intelligent=site_intelligent,
                mongo_repo=mongo_repo,
            )
            rows.append(row)

            scraped = row["scraped"]
            new = row["new"]
            ec = 0 if row["status"] == "ok" else 1
            print(
                f"site={site_id} ec={ec} scraped={scraped} new={new} "
                f"elapsed={row['seconds']}s"
            )
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        total_seconds = int(time.monotonic() - total_started)
        finished_at = datetime.now(TZ_CN)
        log_file.write(f"total_seconds={total_seconds}\n")
        log_file.write(f"finished_at={finished_at.isoformat(timespec='seconds')}\n")
        log_file.write(f"RESULTS_FILE={tsv_path}\n")
        log_file.close()
        if mongo_repo:
            mongo_repo.close()

    with tsv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["site_id", "status", "scraped", "new", "seconds", "error"])
        for row in rows:
            writer.writerow(
                [
                    row["site_id"],
                    row["status"],
                    row["scraped"],
                    row["new"],
                    row["seconds"],
                    row["error"],
                ]
            )

    failed = sum(1 for r in rows if r["status"] == "fail")
    print(f"\n完成 {len(rows)} 站，失败 {failed} 站，耗时 {total_seconds}s")
    print(f"日志: {log_path}")
    print(f"汇总: {tsv_path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
