#!/usr/bin/env python3
"""Agent 智能爬取 CLI — 实时输出事件到 stdout。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent.task_manager import get_task_manager

LEVEL_PREFIX = {
    "info": "[INFO]",
    "warn": "[WARN]",
    "error": "[ERR ]",
    "success": "[ OK ]",
    "debug": "[DBG ]",
}


def _print_event(event_dict: dict) -> None:
    level = event_dict.get("level", "info")
    prefix = LEVEL_PREFIX.get(level, "[????]")
    ts = event_dict.get("timestamp", "")[:19]
    etype = event_dict.get("type", "log")
    msg = event_dict.get("message", "")
    print(f"{ts} {prefix} [{etype}] {msg}")
    if event_dict.get("data") and etype in ("analyze", "done", "save"):
        print(f"         data: {json.dumps(event_dict['data'], ensure_ascii=False)[:200]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 智能爬取")
    parser.add_argument("site_id", nargs="?", help="sites.yaml 中的站点 ID，如 ccgp_national")
    parser.add_argument("--url", help="自定义站点 URL")
    parser.add_argument("--site-id", dest="custom_site_id", help="自定义站点 ID（配合 --url）")
    parser.add_argument("--max-items", type=int, default=10, help="最大抓取条数")
    args = parser.parse_args()

    if not args.site_id and not args.url:
        parser.error("需提供 site_id 或 --url")

    mgr = get_task_manager()
    site_id = args.custom_site_id or args.site_id

    print(f"=== Agent 智能爬取 ===")
    if site_id:
        print(f"站点: {site_id}")
    if args.url:
        print(f"URL: {args.url}")
    print()

    task = await mgr.create_task(
        site_id=site_id,
        url=args.url,
        max_items=args.max_items,
    )
    print(f"任务已创建: {task.task_id}")
    print("-" * 60)

    async for event in mgr.stream_events(task.task_id):
        _print_event(event.to_sse_dict())
        if event.type == "done":
            break
        if event.type == "error" and event.level == "error":
            break

    final = mgr.get_task(task.task_id)
    if final:
        print("-" * 60)
        print(f"状态: {final.status.value}")
        if final.plan:
            print(f"策略: {final.plan.list_strategy.value}")
            if final.plan.use_existing_adapter:
                print(f"适配器: {final.plan.adapter_name}")
        if final.result:
            print(f"结果: {json.dumps(final.result, ensure_ascii=False)}")
        if final.error:
            print(f"错误: {final.error}")
            sys.exit(1)

    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
