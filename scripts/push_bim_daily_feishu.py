#!/usr/bin/env python3
"""生成 BIM 招标日报并推送至飞书（私信或群 Webhook）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_daily_brief_service import push_bim_daily_brief
from src.core.timezone_utils import APP_TZ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("push_bim_daily_feishu")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIM 招标日报飞书推送")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD；默认按 cron 规则自动解析")
    parser.add_argument(
        "--yesterday",
        action="store_true",
        help="强制统计昨日单日（周一也仅昨日，覆盖默认周末窗口）",
    )
    parser.add_argument("--days", type=int, default=1, help="统计窗口天数，默认 1；与 --date 联用可覆盖自动窗口")
    parser.add_argument("--top-n", type=int, default=10, help="Top N 条标讯，默认 10")
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

    auto_window = args.date is None and not args.yesterday and args.days == 1
    target_date = args.date
    if args.yesterday:
        target_date = (datetime.now(APP_TZ).date() - timedelta(days=1)).isoformat()

    result = push_bim_daily_brief(
        target_date,
        days=args.days,
        top_n=args.top_n,
        dry_run=args.dry_run,
        channel=args.channel,
        auto_window=auto_window,
    )

    brief = result.get("brief") or {}
    output = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "reason": result.get("reason"),
        "channel": result.get("channel"),
        "date_start": brief.get("date"),
        "date_end": brief.get("date_end"),
        "window_start_label": brief.get("window_start_label"),
        "window_end_label": brief.get("window_end_label"),
        "window_label": brief.get("window_label"),
        "is_weekend_daily": brief.get("is_weekend_daily"),
        "bim_count": brief.get("bim_count"),
        "by_site": brief.get("by_site"),
        "amount_stats": brief.get("amount_stats"),
        "response": result.get("response"),
    }

    if args.dry_run or result.get("skipped"):
        print(json.dumps({**output, "brief": brief}, ensure_ascii=False, indent=2))

    if result.get("ok"):
        if result.get("skipped"):
            logger.info("日报已生成但未推送（dry-run 或未配置飞书）")
        else:
            logger.info(
                "BIM 日报已推送至飞书（channel=%s）：%s，共 %d 条",
                result.get("channel"),
                brief.get("date"),
                brief.get("bim_count", 0),
            )
    else:
        logger.error("BIM 日报推送失败: %s", result.get("error") or result.get("response"))
        sys.exit(1)


if __name__ == "__main__":
    main()
