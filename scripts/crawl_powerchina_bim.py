#!/usr/bin/env python3
"""
爬取 bid.powerchina.cn BIM 相关招标公告全文并入库。

使用 keyWords 参数直接搜索 BIM，获取精确的 27 条结果。
流程：
1. 用 keyWords="BIM" 搜索列表 API
2. 逐页获取所有匹配公告
3. 调用 getInfo/{id} 获取详情正文
4. 通过 Hermes skill_save_notice API 入库

用法:
  python3 scripts/crawl_powerchina_bim.py [--dry-run]
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# ── 配置 ──────────────────────────────────────────
BASE = "https://bid.powerchina.cn"
LIST_URL = f"{BASE}/newcbs/recpro-newmember/BidAnnouncementSummary/list"
DETAIL_URL_TPL = BASE + "/newcbs/recpro-newmember/BidAnnouncementSummary/getInfo/"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
}

SITE_ID = "中国电力建设集团有限公司_公共资源交易服务平台"
SAVE_URL_TPL = ("https://bid.powerchina.cn/notice/detail?id={id}"
                "&type=招采公告&typeName=招采公告&index=0-3"
                "&path=/consult/notice&companyType=3&bidType=1")

HERMES_API = os.environ.get("HERMES_API", "http://127.0.0.1:8090")
SAVE_NOTICE_API = f"{HERMES_API}/api/crawl-agent/skills/execute"


# ── 工具函数 ──────────────────────────────────────

def post_json(url, body, retries=3):
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def save_notice_via_api(title, url, publish_date, content_text=None, content_html=None):
    """通过 Hermes skill_save_notice 接口保存公告"""
    args = {
        "site_id": SITE_ID,
        "title": title,
        "url": url,
        "publish_date": publish_date,
    }
    if content_text:
        args["content_text"] = content_text[:50000]
    if content_html:
        args["content_html"] = content_html[:100000]

    body = {
        "tool": "skill_save_notice",
        "arguments": args,
    }

    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            SAVE_NOTICE_API,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def strip_html(html):
    """简单去除 HTML 标签"""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── 主流程 ────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print(f"爬取 {SITE_ID} BIM 相关招标公告")
    print(f"Dry-run: {dry_run}")
    print("=" * 60)

    # Stage 1: 用 keyWords="BIM" 搜索
    all_bim = []
    page = 1
    page_size = 20
    total = None

    while True:
        body = {
            "pageNum": page,
            "pageSize": page_size,
            "keyWords": "BIM",
            "announcementType": "招采公告",
            "companyType": "3",
        }

        print(f"\n[搜索] 第 {page} 页 (keyWords=BIM)...", end=" ", flush=True)
        try:
            data = post_json(LIST_URL, body)
        except Exception as e:
            print(f"失败: {e}")
            break

        rows = data.get("rows", [])
        if total is None:
            total = data.get("total", 0)
            pages = (total + page_size - 1) // page_size
            print(f"共 {total} 条，约 {pages} 页")
        else:
            print(f"返回 {len(rows)} 条")

        for r in rows:
            all_bim.append({
                "id": str(r["id"]),
                "title": r.get("title", ""),
                "publishTime": r.get("publishTime", ""),
                "source": r.get("source", ""),
            })

        if len(rows) < page_size:
            break
        page += 1
        time.sleep(1)

    print(f"\n{'=' * 60}")
    print(f"共找到 {len(all_bim)} 条 BIM 相关公告")
    for item in all_bim:
        print(f"  ID={item['id']} | {item['publishTime'][:10]} | {item['title'][:70]}")

    if dry_run:
        print(f"\n[Dry-run] 共 {len(all_bim)} 条，未入库")
        return

    # Stage 2: 获取详情并入库
    print(f"\n{'=' * 60}")
    print("开始获取详情并入库...")

    saved = 0
    skipped = 0
    failed = 0

    for i, item in enumerate(all_bim):
        detail_id = item["id"]
        detail_url = DETAIL_URL_TPL + detail_id
        save_url = SAVE_URL_TPL.format(id=detail_id)

        print(f"\n[{i + 1}/{len(all_bim)}] ID={detail_id}", end=" ", flush=True)

        # 获取详情
        try:
            detail = get_json(detail_url)
            data = detail.get("data", {}) or {}
            content_html = data.get("announcementContent", "") or data.get("content", "") or ""
            full_title = data.get("title", item["title"]) or item["title"]
            print(f"| {full_title[:50]}...", flush=True)
        except Exception as e:
            print(f"| 详情获取失败: {e}", flush=True)
            full_title = item["title"]
            content_html = ""

        content_text = strip_html(content_html) if content_html else ""

        # 入库
        result = save_notice_via_api(
            title=full_title,
            url=save_url,
            publish_date=item["publishTime"],
            content_text=content_text or None,
            content_html=content_html or None,
        )

        if result.get("ok"):
            if result.get("saved"):
                saved += 1
                print("    -> 已保存", flush=True)
            else:
                skipped += 1
                print("    -> 跳过 (重复)", flush=True)
        else:
            failed += 1
            print(f"    -> 失败: {result.get('error', 'unknown')}", flush=True)

        time.sleep(2)  # rate limit

    print(f"\n{'=' * 60}")
    print("BIM 爬取完成！")
    print(f"  总数: {len(all_bim)}")
    print(f"  新保存: {saved}")
    print(f"  跳过(重复): {skipped}")
    print(f"  失败: {failed}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
