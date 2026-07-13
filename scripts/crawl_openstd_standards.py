#!/usr/bin/env python3
"""
openstd.samr.gov.cn 强制性国家标准爬取工具

两阶段：
  阶段一：HTTP 列表爬取 + 详情页元数据提取，保存到 MongoDB
  阶段二：WebBridge PDF 在线预览截图（可选，需要浏览器环境）

用法:
    # 仅爬取列表元数据（推荐先跑这个）
    python scripts/crawl_openstd_standards.py --max-pages 1
    python scripts/crawl_openstd_standards.py --max-pages 10
    python scripts/crawl_openstd_standards.py --all

    # 爬取后对指定标准进行 PDF 截图
    python scripts/crawl_openstd_standards.py --screenshot --hcno XXX

    # 同时爬取并截图（非常慢）
    python scripts/crawl_openstd_standards.py --max-pages 1 --screenshot

注意：PDF 截图需要 WebBridge 浏览器扩展已开启（http://127.0.0.1:10086）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 常量 ────────────────────────────────────────────────────────────
SITE_ID = "openstd_national"
BASE_LIST_URL = "https://openstd.samr.gov.cn/bzgk/std/std_list_type"
BASE_DETAIL_URL = "https://openstd.samr.gov.cn/bzgk/std/newGbInfo"
BASE_PREVIEW_URL = "https://openstd.samr.gov.cn/bzgk/std/showGb"
WEBBRIDGE_URL = "http://127.0.0.1:8090"

TOTAL_ITEMS = 6120
PAGE_SIZE = 10
TOTAL_PAGES = (TOTAL_ITEMS + PAGE_SIZE - 1) // PAGE_SIZE  # 612

OUTPUT_DIR = ROOT / "data" / "standards"
SCREENSHOT_DIR = ROOT / "data" / "standards_screenshots"

# ── HTTP 工具 ────────────────────────────────────────────────────────


def http_get(url: str, retries: int = 3, delay: float = 2.0) -> str | None:
    """HTTP GET 请求，带重试"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                return data
        except Exception as e:
            if attempt < retries - 1:
                print(f"  HTTP 失败 ({attempt+1}/{retries}): {e}, {delay}s 后重试...")
                time.sleep(delay)
            else:
                print(f"  HTTP 失败 (最终): {e}")
    return None


# ── 列表页解析 ──────────────────────────────────────────────────────


def parse_list_page(html: str) -> list[dict[str, str]]:
    """从列表页 HTML 提取标准项（去重，每标准一条）"""
    # 先找到 table.result_list
    table_match = re.search(r'<table[^>]*class="[^"]*result_list[^"]*"[^>]*>', html)
    if not table_match:
        print("  未找到 result_list 表格")
        return []

    # 从 table 开始到 </table>
    table_start = table_match.start()
    table_end = html.find("</table>", table_start)
    if table_end < 0:
        table_end = len(html)
    table_html = html[table_start:table_end]

    # 按 <tr> 分割
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)

    items = []
    seen_hcnos = set()

    for row_html in rows:
        # 跳过表头
        if "<th" in row_html:
            continue

        # 提取 hcno
        hcno_match = re.search(r"showInfo\('([A-F0-9]+)'\)", row_html)
        if not hcno_match:
            continue

        hcno = hcno_match.group(1)
        if hcno in seen_hcnos:
            continue
        seen_hcnos.add(hcno)

        # 提取 tds
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)

        item = {
            "hcno": hcno,
            "standard_no": "",
            "caibiao": "",
            "std_name": "",
            "status": "",
            "publish_date": "",
            "implement_date": "",
        }

        for idx, td_content in enumerate(tds):
            text = re.sub(r"<[^>]+>", " ", td_content).strip()
            text = re.sub(r"\s+", " ", text)
            if idx == 1:  # 标准号
                item["standard_no"] = text
            elif idx == 2:  # 是否采标
                text_clean = text.replace(" ", "")
                item["caibiao"] = "采" if "采" in text_clean else ""
            elif idx == 3:  # 标准名称
                item["std_name"] = text
            elif idx == 4:  # 状态
                item["status"] = text
            elif idx == 5:  # 发布日期
                item["publish_date"] = text
            elif idx == 6:  # 实施日期
                item["implement_date"] = text

        items.append(item)

    return items


# ── 详情页解析 ──────────────────────────────────────────────────────


def parse_detail_page(html: str) -> dict[str, str]:
    """从详情页 HTML 提取标准元数据"""
    detail = {}

    # 提取各种字段
    patterns = {
        "standard_no": r"标准号[：:]\s*([^<>\n]+)",
        "chinese_name": r"中文标准名称[：:]\s*([^<>\n]+)",
        "english_name": r"英文标准名称[：:]\s*([^<>\n]+)",
        "status": r"标准状态[：:]\s*([^<>\n]+)",
        "ics": r"ICS[：:]\s*([^<>\n]+)",
        "ccs": r"CCS[：:]\s*([^<>\n]+)",
    }

    for key, pat in patterns.items():
        m = re.search(pat, html)
        if m:
            detail[key] = m.group(1).strip()

    # 检查是否有在线预览
    has_preview = "在线预览" in html and "showGb" in html
    detail["has_online_preview"] = "true" if has_preview else "false"

    # 提取 pdfUrl 如果存在
    pdf_match = re.search(r'showGb[^"]*?hcno=' + re.escape(detail.get("standard_no", "")), html)
    if not pdf_match:
        pdf_match = re.search(r'showGb[^"]*?hcno=([A-F0-9]+)', html)
    if pdf_match:
        detail["preview_url"] = f"/bzgk/std/showGb?type=online&hcno={pdf_match.group(1)}" if pdf_match.lastindex else ""

    return detail


# ── 阶段一：爬取列表 + 详情 ────────────────────────────────────────


def crawl_standards_http(
    max_pages: int = 1,
    fetch_detail: bool = True,
    delay: float = 2.0,
    output_json: bool = False,
) -> dict[str, Any]:
    """HTTP 爬取标准列表和详情"""
    summary = {
        "ok": True,
        "site_id": SITE_ID,
        "max_pages": max_pages,
        "fetched_pages": 0,
        "total_items": 0,
        "details_fetched": 0,
        "saved": 0,
        "skipped": 0,
        "errors": [],
        "pages": [],
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_items = []

    for page in range(1, max_pages + 1):
        print(f"\n{'=' * 60}")
        print(f"第 {page}/{max_pages} 页...")
        print(f"{'=' * 60}")

        url = f"{BASE_LIST_URL}?p.p1={page}&p.p90=circulation_date&p.p91=desc"
        html = http_get(url)
        if not html:
            summary["errors"].append(f"第 {page} 页 HTTP 失败")
            continue

        items = parse_list_page(html)
        print(f"  解析到 {len(items)} 条标准")
        summary["fetched_pages"] += 1
        summary["total_items"] += len(items)

        page_data = {
            "page": page,
            "item_count": len(items),
            "items": items,
        }

        if fetch_detail and items:
            for i, item in enumerate(items):
                hcno = item["hcno"]
                detail_url = f"{BASE_DETAIL_URL}?hcno={hcno}"
                print(f"  [{i+1}/{len(items)}] {item['standard_no']} - {item['std_name'][:30]}")

                detail_html = http_get(detail_url)
                if detail_html:
                    detail = parse_detail_page(detail_html)
                    item["detail"] = detail
                    summary["details_fetched"] += 1
                else:
                    item["detail"] = {}
                    summary["errors"].append(f"详情失败: hcno={hcno}")

                if delay > 0 and i < len(items) - 1:
                    time.sleep(delay)

        all_items.extend(items)

        # 每页保存
        page_path = OUTPUT_DIR / f"standards_page_{page}.json"
        with open(page_path, "w", encoding="utf-8") as f:
            json.dump(page_data, f, ensure_ascii=False, indent=2)
        print(f"  已保存到 {page_path}")

        if delay > 0 and page < max_pages:
            time.sleep(delay)

    # 合并保存
    if all_items:
        merged_path = OUTPUT_DIR / "standards_all.json"
        # 如果是追加模式，读取已有数据合并去重
        existing = []
        if merged_path.exists():
            try:
                with open(merged_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except:
                pass
        existing_hcnos = {e["hcno"] for e in existing if "hcno" in e}
        new_items = [i for i in all_items if i.get("hcno") not in existing_hcnos]
        existing.extend(new_items)
        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"\n合并后共 {len(existing)} 条标准写入 {merged_path}")

    summary["pages"].append({"page_data": page_data if 'page_data' in dir() else {}})

    print(f"\n=== 完成 ===")
    print(f"爬取页数: {summary['fetched_pages']}")
    print(f"标准总数: {summary['total_items']}")
    print(f"详情数: {summary['details_fetched']}")
    print(f"错误: {len(summary['errors'])}")

    if output_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return summary


# ── WebBridge 截图 ──────────────────────────────────────────────────


def webbridge_post(path: str, data: dict | None = None) -> dict[str, Any]:
    """通过 WebBridge API 执行操作"""
    import urllib.request

    url = f"{WEBBRIDGE_URL}{path}"
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_webbridge() -> bool:
    """检查 WebBridge 是否可用"""
    try:
        resp = webbridge_post("/api/crawl-agent/skills/execute", {
            "skill": "webbridge_check",
            "params": {}
        })
        ok = resp.get("data", {}).get("result", {}).get("available", False)
        return ok
    except Exception as e:
        return False


def screenshot_standard(
    hcno: str,
    std_name: str = "",
    detail_url: str | None = None,
    delay: float = 2.0,
    max_pdf_pages: int = 3,
) -> list[str]:
    """对单个标准进行 PDF 在线预览截图

    流程:
    1. 导航到详情页
    2. Hijack window.open → 点击"在线预览"按钮
    3. 在当前标签页打开 PDF 阅读器
    4. 逐页截图（最多 max_pdf_pages 页）
    5. 翻页继续截图

    返回截图路径列表
    """
    if not detail_url:
        detail_url = f"{BASE_DETAIL_URL}?hcno={hcno}"

    label = std_name or hcno
    print(f"\n截图: {label}")

    # 1. 导航到详情页
    resp = webbridge_post("/api/crawl-agent/skills/execute", {
        "skill": "webbridge_navigate",
        "params": {"url": detail_url, "new_tab": False, "session_id": "crawl-agent"}
    })
    if not resp.get("data", {}).get("result", {}).get("ok"):
        print(f"  导航失败: {resp}")
        return []

    time.sleep(delay)

    # 2. Hijack window.open → 点击在线预览
    code_hijack = """
    (function() {
        window.open = function(url, name, features) {
            window.location.href = url;
            return window;
        };
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            if (buttons[i].textContent.indexOf('在线预览') >= 0) {
                buttons[i].click();
                return 'hijacked_and_clicked';
            }
        }
        return 'button_not_found';
    })();
    """
    resp = webbridge_post("/api/crawl-agent/skills/execute", {
        "skill": "webbridge_evaluate",
        "params": {"code": code_hijack, "session_id": "crawl-agent"}
    })
    result = resp.get("data", {}).get("result", {}).get("value", "")
    print(f"  点击结果: {result}")

    if "not_found" in str(result):
        print(f"  错误: 找不到在线预览按钮")
        return []

    # 3. 等待 PDF 阅读器加载（页面会跳转）
    print(f"  等待 PDF 加载...")
    time.sleep(5)

    # 检查是否进入了 PDF 阅读器
    resp = webbridge_post("/api/crawl-agent/skills/execute", {
        "skill": "webbridge_snapshot",
        "params": {"session_id": "crawl-agent"}
    })
    current_url = resp.get("data", {}).get("result", {}).get("url", "")
    current_title = resp.get("data", {}).get("result", {}).get("title", "")
    print(f"  当前页面: {current_title}")
    print(f"  当前URL: {current_url}")

    if "在线预览" not in current_title:
        print(f"  警告: 不在PDF预览页面，尝试直接导航")
        return []

    # 4. 获取总页数
    resp = webbridge_post("/api/crawl-agent/skills/execute", {
        "skill": "webbridge_evaluate",
        "params": {
            "session_id": "crawl-agent",
            "code": """(function() {
                var pageInput = document.getElementById('pageNumber');
                var totalEl = document.getElementById('numPages');
                if (!totalEl) totalEl = document.querySelector('.numPages, .totalPages, [class*="numPages"]');
                return {
                    currentPage: pageInput ? parseInt(pageInput.value) : 0,
                    totalPages: totalEl ? parseInt(totalEl.textContent) : 0
                };
            })()"""
        }
    })
    pdf_info = resp.get("data", {}).get("result", {}).get("value", "{}")
    try:
        pdf_info = json.loads(pdf_info) if isinstance(pdf_info, str) else pdf_info
    except:
        pdf_info = {}
    total_pages = int(pdf_info.get("totalPages", 0))
    print(f"  PDF共 {total_pages} 页，截图前 {min(max_pdf_pages, total_pages or 999)} 页")

    # 5. 逐页截图
    screenshots = []
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', label)[:60]
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    pdf_pages_to_capture = min(max_pdf_pages, total_pages or 1)

    for page in range(1, pdf_pages_to_capture + 1):
        # 截图
        resp = webbridge_post("/api/crawl-agent/skills/execute", {
            "skill": "webbridge_screenshot",
            "params": {
                "session_id": "crawl-agent",
                "format": "jpeg",
                "quality": 85,
            }
        })
        img_path = resp.get("data", {}).get("result", {}).get("image_path", "")
        if img_path:
            screenshots.append(img_path)
            print(f"  第{page}页截图: {os.path.basename(img_path)}")
            # 复制到截图目录
            import shutil
            dst = SCREENSHOT_DIR / f"{safe_name}_p{page}.jpg"
            try:
                shutil.copy2(img_path, dst)
                print(f"     已保存: {dst}")
            except:
                pass
        else:
            print(f"  第{page}页截图失败")

        # 翻到下一页
        if page < total_pages:
            code_next = """
            (function() {
                var nextBtn = document.getElementById('next');
                if (nextBtn && !nextBtn.disabled) {
                    nextBtn.click();
                    return 'clicked_next';
                }
                return 'no_next';
            })();
            """
            resp = webbridge_post("/api/crawl-agent/skills/execute", {
                "skill": "webbridge_evaluate",
                "params": {"code": code_next, "session_id": "crawl-agent"}
            })
            time.sleep(2)

    print(f"  截图完成: {len(screenshots)}/{pdf_pages_to_capture} 页")
    return screenshots


def webbridge_screenshot_all(session_id: str, std_name: str, max_pages: int = 5) -> list[str]:
    """对当前打开的 PDF 阅读器进行多页截图"""
    screenshots = []
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', std_name)[:80]

    for page in range(1, max_pages + 1):
        # 截图
        resp = webbridge_post("/api/crawl-agent/skills/execute", {
            "skill": "webbridge_screenshot",
            "params": {"session_id": session_id, "format": "jpeg", "quality": 85}
        })
        img_path = resp.get("data", {}).get("result", {}).get("image_path", "")
        if img_path:
            screenshots.append(img_path)
            print(f"    第 {page} 页截图: {img_path}")

        # 尝试点击"下一页"
        code_next = """
        (function() {
            var nextBtn = document.querySelector('button:contains("下一页"), [class*="next"], .next, a.next');
            var allBtns = document.querySelectorAll('button');
            for (var i = 0; i < allBtns.length; i++) {
                if (allBtns[i].textContent.includes('下一页')) {
                    allBtns[i].click();
                    return 'clicked next';
                }
            }
            return 'no next page button';
        })();
        """
        time.sleep(1)

        if page < max_pages:
            # 尝试点击下一页
            try:
                resp = webbridge_post("/api/crawl-agent/skills/execute", {
                    "skill": "webbridge_evaluate",
                    "params": {"code": code_next, "session_id": session_id}
                })
                time.sleep(2)
            except:
                break

    return screenshots


# ── 阶段二：批量截图 ────────────────────────────────────────────────


def batch_screenshot(
    items_file: str | None = None,
    max_items: int = 1,
    delay: float = 3.0,
) -> dict[str, Any]:
    """从已有 JSON 文件读取标准列表，批量截图"""
    if not check_webbridge():
        return {"ok": False, "error": "WebBridge 不可用，请确保浏览器扩展已启动"}

    print("WebBridge 可用")
    print(f"{'=' * 60}")

    # 读取标准列表
    if items_file:
        path = Path(items_file)
    else:
        path = OUTPUT_DIR / "standards_all.json"

    if not path.exists():
        # 尝试最新页
        latest = sorted(OUTPUT_DIR.glob("standards_page_*.json"))
        if latest:
            path = latest[-1]
        else:
            return {"ok": False, "error": f"未找到标准列表文件，先运行 --max-pages N"}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "items" in data:
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        items = [data]

    print(f"共 {len(items)} 条标准，截图前 {max_items} 条")
    results = []

    for i, item in enumerate(items[:max_items]):
        hcno = item.get("hcno", "")
        std_name = item.get("std_name") or item.get("name", "")
        std_no = item.get("standard_no") or item.get("stdNo", "")

        if not hcno:
            print(f"  [{i+1}/{max_items}] 跳过（无 hcno）: {std_name}")
            continue

        label = f"{std_no} {std_name}"[:60]

        img_path = screenshot_standard(
            hcno=hcno,
            std_name=f"{std_no}_{std_name}",
            detail_url=item.get("detailFullUrl") or (
                f"{BASE_DETAIL_URL}?hcno={hcno}"
            ),
            delay=delay,
        )

        results.append({
            "hcno": hcno,
            "standard_no": std_no,
            "std_name": std_name,
            "screenshot": img_path or "",
        })

        if delay > 0 and i < max_items - 1:
            time.sleep(delay * 2)

    summary = {
        "ok": True,
        "total": len(items[:max_items]),
        "screenshots": len([r for r in results if r["screenshot"]]),
        "results": results,
    }

    print(f"\n=== 截图完成 ===")
    print(f"截图成功: {summary['screenshots']}/{summary['total']}")

    return summary


# ── 主入口 ──────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="openstd.samr.gov.cn 强制性国家标准爬取"
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="最大列表页数（0=仅查看信息，1+=开始爬取）"
    )
    parser.add_argument(
        "--no-detail", action="store_true",
        help="不抓详情页元数据"
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="请求间隔秒数（默认 2.0）"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出 JSON 摘要"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="爬取全部 612 页"
    )

    # 截图相关
    parser.add_argument(
        "--screenshot", action="store_true",
        help="对已爬取的标准进行 PDF 在线预览截图"
    )
    parser.add_argument(
        "--hcno", type=str, default="",
        help="对指定 hcno 截图"
    )
    parser.add_argument(
        "--from-file", type=str, default="",
        help="从指定 JSON 文件读取标准列表进行截图"
    )
    parser.add_argument(
        "--max-items", type=int, default=1,
        help="截图最大条数（默认 1）"
    )

    args = parser.parse_args()

    # ── 截图模式 ──
    if args.screenshot or args.hcno:
        if args.hcno:
            print(f"=== 对指定标准截图: hcno={args.hcno} ===")
            if not check_webbridge():
                print("错误: WebBridge 不可用")
                return 1
            screenshot_standard(
                hcno=args.hcno,
                std_name=args.hcno,
                delay=args.delay,
            )
            return 0
        else:
            result = batch_screenshot(
                items_file=args.from_file or None,
                max_items=args.max_items,
                delay=args.delay,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1

    # ── 列表爬取模式 ──
    if args.all:
        max_pages = TOTAL_PAGES
    elif args.max_pages > 0:
        max_pages = args.max_pages
    else:
        print("openstd.samr.gov.cn 强制性国家标准")
        print(f"  总标准数: {TOTAL_ITEMS}")
        print(f"  总页数: {TOTAL_PAGES}")
        print(f"  每页: {PAGE_SIZE} 条")
        print(f"  输出目录: {OUTPUT_DIR}")
        print(f"\n用法示例:")
        print(f"  python scripts/crawl_openstd_standards.py --max-pages 1     # 爬第1页（10条）")
        print(f"  python scripts/crawl_openstd_standards.py --max-pages 3     # 爬前3页")
        print(f"  python scripts/crawl_openstd_standards.py --all             # 爬全部612页")
        print(f"  python scripts/crawl_openstd_standards.py --screenshot --max-items 1  # 截图1条")
        print(f"  python scripts/crawl_openstd_standards.py --hcno 524FDF24793BCC6D2BE9CDA52A418114  # 指定hcno截图")
        return 0

    result = crawl_standards_http(
        max_pages=max_pages,
        fetch_detail=not args.no_detail,
        delay=args.delay,
        output_json=args.json,
    )

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
