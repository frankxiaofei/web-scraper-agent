#!/usr/bin/env python3
"""
新基建通 微信公众号每日爬取脚本
策略：
  1. 搜狗微信搜索（优先）：搜索公众号名称，获取最新文章链接
  2. 链式发现（备用）：从已爬文章页面提取文章链接
每天运行，增量爬取并去重保存到 notices 集合（公告列表可见）。

也可通过标准同步管道触发：POST /api/sync/wechat_xjjt/trigger
"""

from __future__ import annotations

import argparse
import os
import sys

# 确保项目根在 sys.path
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from src.core.wechat_crawl import crawl_new_articles, resolve_wechat_nickname
from src.web.crawl_agent_skills import save_notice

# 兼容旧脚本常量
NICKNAME = "新基建通"
SITE_ID = "wechat_xjjt"
ACCOUNT_ID = SITE_ID
KNOWN_START_URL = "https://mp.weixin.qq.com/s/FHC5sQeWTsSkvqBuO7j_nA"


def save_to_db(doc: dict, dry_run: bool = False) -> bool:
    """通过 save_notice 管道保存到 notices 集合（去重）。"""
    if dry_run:
        print(f"  [DRY-RUN] 将保存: {doc['title']} | {doc['url']}")
        return True

    try:
        result = save_notice(
            site_id=SITE_ID,
            title=doc.get("title", ""),
            url=doc.get("url", ""),
            publish_date=doc.get("publish_time", ""),
            content_text=doc.get("content", ""),
            content_html=doc.get("content_html", ""),
            attachments=doc.get("attachments") or [],
        )
        saved = result.get("ok", False) and result.get("saved", False)
        if saved:
            print(f"  [OK] 已保存: {doc['title']}")
        else:
            reason = "重复" if result.get("duplicate") else result.get("error", "未知")
            print(f"  [SKIP] {reason}: {doc['title']}")
        return saved
    except Exception as e:
        print(f"  [ERROR] 保存失败: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description=f"爬取{NICKNAME}微信公众号文章")
    parser.add_argument(
        "--max-articles", type=int, default=15, help="每次最多爬取文章数（默认 15）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只发现不保存")
    parser.add_argument(
        "--sogou-only", action="store_true", help="仅使用搜狗搜索，跳过链式发现"
    )
    parser.add_argument(
        "--chain-only", action="store_true", help="仅使用链式发现，跳过搜狗搜索"
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="重新提取 seen_urls 中已有文章（图片/正文回填）",
    )
    args = parser.parse_args()

    print(f"={' 新基建通公众号爬取 ':=^60}")
    print(f"  模式: {'DRY RUN（不保存）' if args.dry_run else '正式运行'}")
    print(f"  最多爬取: {args.max_articles} 篇")
    print()

    site_config = {
        "id": SITE_ID,
        "name": f"{NICKNAME}（微信公众号）",
        "wechat_nickname": NICKNAME,
    }
    nickname = resolve_wechat_nickname(site_config)

    articles = crawl_new_articles(
        site_id=SITE_ID,
        nickname=nickname,
        seed_url=KNOWN_START_URL,
        max_articles=args.max_articles,
        use_sogou=args.sogou_only if hasattr(args, 'sogou_only') else False,
        use_webbridge=not getattr(args, 'chain_only', False),
        use_chain=not getattr(args, 'sogou_only', False),
        force_refresh=args.force_refresh,
    )

    if not articles:
        print("\n[INFO] 没有新文章" + ("（force-refresh 也未命中）" if args.force_refresh else "") + "，完成！")
        return

    print(f"\n[INFO] 本次计划爬取: {len(articles)} 篇")
    success_count = 0
    for i, article in enumerate(articles):
        print(f"\n  [{i+1}/{len(articles)}] {article.get('title', '')[:40]}")
        if save_to_db(article, dry_run=args.dry_run):
            success_count += 1

    print(f"\n={' 完成 ':=^60}")
    print(f"  爬取尝试: {len(articles)} 篇")
    print(f"  成功保存: {success_count} 篇")
    if args.dry_run:
        print("  (DRY RUN - 未写入数据库)")
    print()


if __name__ == "__main__":
    main()
