#!/usr/bin/env python3
"""测试搜狗搜索发现小伊评科技的文章。"""
import sys
import os
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

from src.core.wechat_crawl import search_via_sogou, extract_links_from_page, discover_article_urls, load_seen

site_id = "wechat_xiaoyipingkeji"
nickname = "小伊评科技"
seed_url = "https://mp.weixin.qq.com/s/UMpDoV0xIZZmEN3Z-G_BMw"

# 1. 搜狗搜索
print("=== 搜狗搜索 ===")
sogou_urls = search_via_sogou(nickname, max_pages=2)
print(f"搜狗发现 {len(sogou_urls)} 个 URL:")
for u in sorted(sogou_urls):
    print(f"  {u}")

# 2. 链式发现
print("\n=== 链式发现 ===")
seen = load_seen(site_id)
print(f"已爬记录: {len(seen)} 条")

chain_urls = discover_article_urls(
    site_id=site_id, nickname=nickname, seed_url=seed_url,
    seen=seen, use_sogou=False, use_chain=True
)
print(f"链式发现 {len(chain_urls)} 个 URL:")
for u in sorted(chain_urls):
    print(f"  {u}")
