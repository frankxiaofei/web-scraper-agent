#!/usr/bin/env python3
"""探测 dlzb 统一平台列表 API / DOM 结构。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SITES = [
    ("tjbid", "https://tjbid.dlzb.com/"),
    ("zgjtjs", "https://zgjtjs.dlzb.com/"),
    ("zhfdc", "https://zhfdc.dlzb.com/"),
]


async def probe(label: str, url: str) -> dict:
    from playwright.async_api import async_playwright

    chrome = ROOT / "docs/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    captured: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=str(chrome))
        page = await browser.new_page()

        async def on_response(response):
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            try:
                body = await response.json()
            except Exception:
                return
            text = json.dumps(body, ensure_ascii=False)[:500]
            if any(k in text for k in ("noticeTitle", "pubTitle", "records", "listWithHomePage", "projectName")):
                captured.append({"url": response.url, "preview": text[:400]})

        page.on("response", on_response)
        await page.goto(url, wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(4000)

        dom = await page.evaluate(
            """() => {
              const rows = document.querySelectorAll('table tr, .el-table__row, [class*="notice"], [class*="list"] li');
              const links = [...document.querySelectorAll('a[href]')].slice(0, 30).map(a => ({
                text: (a.innerText||'').trim().slice(0,80),
                href: a.getAttribute('href')
              })).filter(x => x.text.length > 4);
              return { rowCount: rows.length, sampleLinks: links.slice(0, 8), title: document.title, hash: location.hash };
            }"""
        )
        await browser.close()

    return {"label": label, "url": url, "apis": captured[:8], "dom": dom}


async def main() -> None:
    results = []
    for label, url in SITES:
        print(f"探测 {label} ...", flush=True)
        try:
            results.append(await probe(label, url))
        except Exception as exc:
            results.append({"label": label, "url": url, "error": str(exc)})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
