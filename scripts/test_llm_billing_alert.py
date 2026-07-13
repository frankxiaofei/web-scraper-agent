#!/usr/bin/env python3
"""手动触发 LLM 欠费告警飞书私信（dry-run 或真实推送）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.llm_alert_service import (  # noqa: E402
    reset_llm_alert_cooldown,
    send_llm_billing_alert_to_admin,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="测试 LLM 欠费飞书告警")
    parser.add_argument(
        "--user-id",
        default="li_xf10",
        help="飞书 user_id（默认 li_xf10）",
    )
    parser.add_argument(
        "--source",
        default="manual_test",
        help="告警来源标识",
    )
    parser.add_argument(
        "--detail",
        default="[测试] LLM HTTP 402 insufficient balance，请充值",
        help="模拟错误详情",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印告警内容，不实际发送",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="真实发送飞书私信（默认 dry-run）",
    )
    args = parser.parse_args()

    dry_run = not args.real
    if args.dry_run:
        dry_run = True

    reset_llm_alert_cooldown()
    result = send_llm_billing_alert_to_admin(
        args.detail,
        user_id=args.user_id,
        source=args.source,
        dry_run=dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok") and not result.get("skipped"):
        sys.exit(1)


if __name__ == "__main__":
    main()
