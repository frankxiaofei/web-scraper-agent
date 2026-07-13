#!/usr/bin/env python3
"""
华能电子商务平台 - BIM全文搜索爬取脚本
通过WebBridge（用户真实浏览器带登录态）搜全文BIM并抓取列表

使用方式: python3 scripts/sync_chng_bim_webbridge.py
依赖: WebBridge 必须在线（webbridge daemon + browser extension）
"""

import sys
import os
import json
import time
import urllib.request
import urllib.parse

WEBBRIDGE_URL = "http://127.0.0.1:10086"
SITE_ID = "中国华能集团有限公司_电子商务平台"
ENTRY_URL = "https://ec.chng.com.cn/channel/home/#/purchase?checked=3"
MAX_PAGES = 4

def webbridge_call(method, params=None):
    """Call WebBridge execute API"""
    url = f"{WEBBRIDGE_URL}/api/v1/execute"
    payload = {
        "command": f"webbridge.{method}",
        "arguments": params or {}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"WebBridge call {method} failed: {e}")
        return None

def save_notice(title, url, publish_date):
    """Call Hermes API to save notice"""
    api_url = "http://127.0.0.1:8090/api/crawl-agent/skills/execute"
    payload = {
        "skill": "skill_save_notice",
        "arguments": {
            "site_id": SITE_ID,
            "title": title,
            "url": url,
            "publish_date": publish_date
        }
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  save_notice failed: {e}")
        return None

def main():
    print("=" * 60)
    print(f"华能电子商务平台 BIM全文搜索爬取")
    print(f"站点: {SITE_ID}")
    print("=" * 60)

    # Step 1: Navigate to entry URL
    print("\n[1/6] 打开采购专栏页面...")
    result = webbridge_call("navigate", {
        "url": ENTRY_URL,
        "group_title": "ec.chng.com.cn BIM每日爬取"
    })
    if not result or not result.get("ok"):
        print("ERROR: 导航失败，WebBridge是否可用？")
        sys.exit(1)
    print("  页面已打开")

    # Step 2: Wait for page to load
    print("\n[2/6] 等待页面加载...")
    webbridge_call("wait", {"seconds": 3})

    # Step 3: Fill search box with "BIM"
    print("\n[3/6] 输入BIM搜索关键词...")
    result = webbridge_call("fill", {
        "selector": "input[role='textbox'][name='搜索']",
        "value": "BIM"
    })
    if not result or not result.get("ok"):
        print("  WARN: 填充搜索框失败，尝试备用方法")
    else:
        print("  已输入BIM")

    # Step 4: Click "搜全文" button
    print("\n[4/6] 点击搜全文按钮...")
    time.sleep(1)
    result = webbridge_call("click", {
        "selector": "button:has-text('搜全文')"
    })
    if not result or not result.get("ok"):
        # Try ref-based click
        result = webbridge_call("click", {"selector": "@e5"})
    webbridge_call("wait", {"seconds": 3})
    print("  已搜索")

    # Step 5: Extract items from all pages
    print("\n[5/6] 逐页抓取BIM公告...")
    total_saved = 0
    total_duplicates = 0
    
    for page_num in range(1, MAX_PAGES + 1):
        print(f"\n  --- 第 {page_num} 页 ---")
        
        if page_num > 1:
            # Click page number
            result = webbridge_call("evaluate", {
                "code": f"""
                (() => {{
                    let items = document.querySelectorAll('.ant-pagination li');
                    for(let li of items) {{
                        if(li.innerText.trim() === '{page_num}') {{
                            let a = li.querySelector('a');
                            if(a) a.click();
                            else li.click();
                            return 'clicked page {page_num}';
                        }}
                    }}
                    return 'page {page_num} not found';
                }})()
                """
            })
            webbridge_call("wait", {"seconds": 2})
        
        # Extract items from current page
        result = webbridge_call("evaluate", {
            "code": """
            (() => {
                let rows = document.querySelectorAll('.ant-table-row');
                return JSON.stringify(Array.from(rows).map(row => {
                    let titleEl = row.querySelector('.list-text');
                    let dateEl = row.querySelector('td:last-child p');
                    let key = row.getAttribute('data-row-key');
                    return {
                        title: titleEl?.innerText?.trim() || '',
                        date: dateEl?.innerText?.trim() || '',
                        id: key
                    };
                }));
            })()
            """
        })
        
        if not result or not result.get("ok"):
            print(f"  WARN: 无法提取第{page_num}页数据")
            continue
        
        items_text = result["result"]["value"]
        try:
            items = json.loads(items_text)
        except:
            print(f"  解析失败: {items_text[:100]}")
            continue
        
        print(f"  找到 {len(items)} 条公告")
        
        for item in items:
            title = item["title"]
            date = item["date"]
            id_ = item["id"]
            if not title:
                continue
            detail_url = f"https://ec.chng.com.cn/channel/home/#/purchase/detail/{id_}"
            
            resp = save_notice(title, detail_url, date)
            if resp:
                saved = resp.get("data", {}).get("result", {}).get("saved", False)
                duplicate = resp.get("data", {}).get("result", {}).get("duplicate", False)
                if saved and not duplicate:
                    total_saved += 1
                    print(f"  ✓ [{date}] {title[:40]}...")
                elif duplicate:
                    total_duplicates += 1
                else:
                    print(f"  ? [{date}] {title[:30]}... (status: unknown)")
            else:
                print(f"  ✗ [{date}] {title[:30]}... (save failed)")
            
            time.sleep(0.5)  # rate limit

    # Step 6: Close session
    print(f"\n[6/6] 关闭WebBridge会话...")
    webbridge_call("close", {"close_session": True})
    
    print("\n" + "=" * 60)
    print(f"爬取完成！")
    print(f"  新增: {total_saved} 条")
    print(f"  重复: {total_duplicates} 条")
    print("=" * 60)

if __name__ == "__main__":
    main()
