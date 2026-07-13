#!/usr/bin/env python3
"""生成采集周报并推送至飞书（私信或群 Webhook）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.weekly_crawl_brief_service import push_weekly_crawl_brief

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("push_weekly_crawl_feishu")


def main() -> None:
    parser = argparse.ArgumentParser(description="采集周报飞书推送")
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="统计结束日期 YYYY-MM-DD，默认今天",
    )
    parser.add_argument("--days", type=int, default=7, help="统计窗口天数，默认 7")
    parser.add_argument("--top-n", type=int, default=8, help="BIM Top N 条，默认 8")
    parser.add_argument(
        "--channel",
        choices=("auto", "im", "webhook"),
        default="auto",
        help="推送通道：auto=有 App 凭证则私信否则 Webhook；im=开放平台私信；webhook=群机器人",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不发送，打印简报 JSON",
    )
    args = parser.parse_args()

    result = push_weekly_crawl_brief(
        args.end_date,
        days=args.days,
        top_n=args.top_n,
        dry_run=args.dry_run,
        channel=args.channel,
    )

    brief = result.get("brief") or {}
    output = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "channel": result.get("channel"),
        "date_start": brief.get("date_start"),
        "date_end": brief.get("date_end"),
        "total_notices": brief.get("total_notices"),
        "bim_count_crawl": brief.get("bim_count_crawl"),
        "bim_count_publish": brief.get("bim_count_publish"),
        "run_stats": brief.get("run_stats"),
    }

    if args.dry_run or result.get("skipped"):
        print(json.dumps({**output, "brief": brief}, ensure_ascii=False, indent=2))

    if result.get("ok"):
        if result.get("skipped"):
            logger.info("周报已生成但未推送（dry-run 或未配置飞书）")
        else:
            logger.info(
                "周报已推送至飞书（channel=%s）：%s ~ %s，新采集 %d 条",
                result.get("channel"),
                brief.get("date_start"),
                brief.get("date_end"),
                brief.get("total_notices", 0),
            )
    else:
        logger.error("周报推送失败: %s", result.get("error") or result.get("response"))
        sys.exit(1)


if __name__ == "__main__":
    main()
