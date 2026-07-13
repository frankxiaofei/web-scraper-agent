#!/usr/bin/env python3
"""新智元微信公众号批量爬取脚本 - 通过文章URL直接提取并保存。"""
from __future__ import annotations

import argparse
import os
import sys
import time

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from src.core.wechat_crawl import extract_article, load_seen, save_seen
from src.web.crawl_agent_skills import save_notice

NICKNAME = "新智元"
SITE_ID = "wechat_xzy"

# 已知的新智元文章URL（通过搜索发现）
KNOWN_URLS = [
    "https://mp.weixin.qq.com/s/fgwYd5b-UVHM1xYRxY3A8g",
    "https://mp.weixin.qq.com/s/1Dj3Ieeg9saviIAc33tKEQ",
    "https://mp.weixin.qq.com/s/MSUgMz9Ns2dXYXF5N9zy0A",
    "https://mp.weixin.qq.com/s/YaMK8nWWYYu-eLGWDOJmTw",
    "https://mp.weixin.qq.com/s/HbbcctY2tckr4qXzFYkZtw",
    "https://mp.weixin.qq.com/s/UZsQ8k9_O1NUYScZDB4NLg",
    "https://mp.weixin.qq.com/s/NrK1fbzqHTmqbz84IeZ9gQ",
    "https://mp.weixin.qq.com/s/BLaIJj99VFCk6e2ispgpHw",
]

# 排除非文章页面
EXCLUDE_URLS = {
    "https://mp.weixin.qq.com/s/_2kC-fXw7UjneZSrsC9CVQ",  # 投诉指引
}


def save_to_db(doc: dict, dry_run: bool = False) -> bool:
    """保存到 notices 集合（去重）。"""
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
    parser = argparse.ArgumentParser(description=f"手动爬取{NICKNAME}微信公众号文章")
    parser.add_argument("--max-articles", type=int, default=15, help="每次最多爬取文章数（默认 15）")
    parser.add_argument("--dry-run", action="store_true", help="只发现不保存")
    parser.add_argument("--all-known", action="store_true", help="爬取所有已知URL（含已爬）")
    args = parser.parse_args()

    print(f"={' 新智元公众号批量爬取 ':=^60}")
    print(f"  模式: {'DRY RUN（不保存）' if args.dry_run else '正式运行'}")
    print(f"  最多爬取: {args.max_articles} 篇")
    print()

    seen = load_seen(SITE_ID)
    print(f"  已爬: {len(seen)} 篇")

    # 确定要爬取的URL
    urls_to_crawl = []
    for url in KNOWN_URLS:
        if url in EXCLUDE_URLS:
            continue
        if url in seen and not args.all_known:
            continue
        urls_to_crawl.append(url)

    urls_to_crawl = urls_to_crawl[:args.max_articles]
    print(f"  本次计划爬取: {len(urls_to_crawl)} 篇\n")

    if not urls_to_crawl:
        print("[INFO] 没有新文章，完成！")
        return

    success_count = 0
    for i, url in enumerate(urls_to_crawl):
        print(f"  [{i+1}/{len(urls_to_crawl)}] 正在提取: {url[:60]}...")
        article = extract_article(url, site_id=SITE_ID)
        if not article or not article.get("title"):
            print(f"  [ERROR] 提取失败: {url}")
            continue

        print(f"    标题: {article.get('title', '')[:40]}")
        print(f"    时间: {article.get('publish_time', '')}")

        seen.add(url)
        if save_to_db(article, dry_run=args.dry_run):
            success_count += 1
        time.sleep(3)

    save_seen(SITE_ID, seen)

    print(f"\n={' 完成 ':=^60}")
    print(f"  尝试爬取: {len(urls_to_crawl)} 篇")
    print(f"  成功保存: {success_count} 篇")
    if args.dry_run:
        print("  (DRY RUN - 未写入数据库)")
    print()


if __name__ == "__main__":
    main()
