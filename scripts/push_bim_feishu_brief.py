#!/usr/bin/env python3
"""生成 BIM 招标日报并推送至飞书（私信或群 Webhook）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_daily_brief_service import push_bim_daily_brief

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("push_bim_feishu_brief")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIM 招标日报飞书推送")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--days", type=int, default=1, help="统计窗口天数，默认 1")
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

    result = push_bim_daily_brief(
        args.date,
        days=args.days,
        top_n=args.top_n,
        dry_run=args.dry_run,
        channel=args.channel,
    )

    output = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "reason": result.get("reason"),
        "channel": result.get("channel"),
        "brief": result.get("brief"),
    }
    if args.dry_run or result.get("skipped"):
        print(json.dumps(output, ensure_ascii=False, indent=2))

    if result.get("ok"):
        if result.get("skipped"):
            logger.info("简报已生成但未推送（dry-run 或未配置飞书）")
        else:
            logger.info("简报已推送至飞书（channel=%s）", result.get("channel"))
    else:
        logger.error("简报推送失败: %s", result.get("error") or result.get("response"))
        sys.exit(1)


if __name__ == "__main__":
    main()
