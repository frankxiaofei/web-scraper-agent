#!/usr/bin/env python3
"""生成工程大模型应用简报并私信推送给指定飞书用户。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.engineering_llm_report_service import push_engineering_llm_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("push_engineering_llm_report")


def main() -> None:
    parser = argparse.ArgumentParser(description="工程大模型应用报告飞书私信推送")
    parser.add_argument(
        "--users",
        type=str,
        required=True,
        help="飞书 user_id 列表，逗号分隔，如 yuan_jp,li_xf10",
    )
    parser.add_argument("--days", type=int, default=1, help="统计天数，默认 1")
    parser.add_argument("--top-n", type=int, default=10, help="Top N 条标讯，默认 10")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不发送，打印简报 JSON",
    )
    args = parser.parse_args()

    user_ids = [u.strip() for u in args.users.split(",") if u.strip()]
    result = push_engineering_llm_report(
        feishu_user_ids=user_ids,
        days=args.days,
        top_n=args.top_n,
        dry_run=args.dry_run,
    )

    brief = result.get("brief") or {}
    output = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "reason": result.get("reason"),
        "pushed_count": result.get("pushed_count"),
        "feishu_user_ids": result.get("feishu_user_ids"),
        "count": brief.get("count"),
        "date": brief.get("date"),
        "date_end": brief.get("date_end"),
    }

    if args.dry_run or result.get("skipped"):
        print(json.dumps({**output, "brief": brief}, ensure_ascii=False, indent=2))

    if result.get("ok"):
        if result.get("skipped"):
            logger.info("简报已生成但未推送（dry-run 或未配置飞书 IM）")
        else:
            logger.info(
                "工程大模型应用报告已推送 %d 人，共 %d 条标讯",
                result.get("pushed_count", 0),
                brief.get("count", 0),
            )
    else:
        logger.error("推送失败: %s", result.get("error") or result.get("response"))
        sys.exit(1)


if __name__ == "__main__":
    main()
