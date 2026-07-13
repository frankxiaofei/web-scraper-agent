#!/usr/bin/env python3
"""生成 BIM 招标周报并推送至飞书（私信或群 Webhook）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_daily_brief_service import push_bim_daily_brief, push_bim_weekly_brief

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("push_bim_weekly_feishu")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIM 招标日报/周报飞书推送")
    parser.add_argument(
        "--mode",
        choices=("weekly", "daily"),
        default="weekly",
        help="推送模式：weekly=周五周报（本周+近一月）；daily=单日日报",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="统计结束日期 YYYY-MM-DD，默认今天（daily 模式可用 --date）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="日报目标日期 YYYY-MM-DD（仅 --mode daily）",
    )
    parser.add_argument("--days", type=int, default=30, help="滚动窗口天数，默认 30（近一月）；--last-week 时忽略")
    parser.add_argument(
        "--last-week",
        action="store_true",
        help="统计上一自然周（周一至周日）；手动补推时使用",
    )
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

    if args.mode == "daily":
        result = push_bim_daily_brief(
            args.date or args.end_date,
            days=1,
            top_n=args.top_n,
            dry_run=args.dry_run,
            channel=args.channel,
        )
    else:
        result = push_bim_weekly_brief(
            args.end_date,
            days=args.days,
            top_n=args.top_n,
            dry_run=args.dry_run,
            channel=args.channel,
            last_week=args.last_week,
        )

    brief = result.get("brief") or {}
    output = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "reason": result.get("reason"),
        "channel": result.get("channel"),
        "date_start": brief.get("date"),
        "date_end": brief.get("date_end"),
        "bim_count": brief.get("bim_count"),
        "week_new_count": brief.get("week_new_count"),
        "month_new_count": brief.get("month_new_count"),
        "by_site": brief.get("by_site"),
        "amount_stats": brief.get("amount_stats"),
        "response": result.get("response"),
    }

    if args.dry_run or result.get("skipped"):
        print(json.dumps({**output, "brief": brief}, ensure_ascii=False, indent=2))

    if result.get("ok"):
        if result.get("skipped"):
            logger.info("周报已生成但未推送（dry-run 或未配置飞书）")
        else:
            logger.info(
                "BIM 周报已推送至飞书（channel=%s）：%s ~ %s，共 %d 条",
                result.get("channel"),
                brief.get("date"),
                brief.get("date_end"),
                brief.get("bim_count", 0),
            )
    else:
        logger.error("BIM 周报推送失败: %s", result.get("error") or result.get("response"))
        sys.exit(1)


if __name__ == "__main__":
    main()
