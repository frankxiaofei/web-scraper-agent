#!/usr/bin/env python3
"""
定时爬取微信公众号文章：https://mp.weixin.qq.com/s/Y7cpE6WcrGYbrQBRm7uftA
多糖多酚植物RNA提取全球第一！8min完成（任何植物）
"""
import requests
import re
import datetime
import json
import os

URL = "https://mp.weixin.qq.com/s/Y7cpE6WcrGYbrQBRm7uftA"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "wechat_article_latest.txt")
HISTORY_DIR = os.path.join(DATA_DIR, "wechat_archive")
os.makedirs(HISTORY_DIR, exist_ok=True)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

resp = requests.get(URL, headers=headers, timeout=30, allow_redirects=True)
resp.encoding = 'utf-8'
html = resp.text

# Extract info
title = ""
og_title = ""
profile_name = ""
publish_time = ""
author = ""

m = re.search(r'<title[^>]*>([^<]+)</title>', html)
if m: title = m.group(1)

m = re.search(r'og:title["\']\s+content=["\']([^"\']+)', html)
if m: og_title = m.group(1)

m = re.search(r'["\']profile_name["\']\s*:\s*["\']([^"\']+)', html)
if m: profile_name = m.group(1)

m = re.search(r'["\']publish_time["\']\s*:\s*(\d+)', html)
if m:
    ts = int(m.group(1))
    publish_time = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

m = re.search(r'["\']author_name["\']\s*:\s*["\']([^"\']+)', html)
if m: author = m.group(1)

# Extract content
content_text = ""
m = re.search(r'id="js_content"[^>]*>([\s\S]*?)</div>\s*<script', html)
if m:
    text = re.sub(r'<br\s*/?>', '\n', m.group(1))
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'</div>', '\n', text)
    text = re.sub(r'</section>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    content_text = '\n'.join(lines)

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
output = f"""=== 公众号文章抓取报告 ===
抓取时间: {now}
URL: {URL}
标题: {og_title or title}
公众号: {profile_name}
发布时间: {publish_time}
作者: {author}

=== 正文内容 ===
{content_text}

=== 抓取完成 ===
"""

# Save latest
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    f.write(output)

# Archive with date stamp
date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
archive_file = os.path.join(HISTORY_DIR, f"wechat_article_{date_str}.txt")
with open(archive_file, 'w', encoding='utf-8') as f:
    f.write(output)

print(f"✅ 抓取完成")
print(f"   标题: {og_title or title}")
print(f"   公众号: {profile_name}")
print(f"   正文: {len(content_text)} 字符")
print(f"   已保存: {OUTPUT_FILE}")
print(f"   已归档: {archive_file}")
