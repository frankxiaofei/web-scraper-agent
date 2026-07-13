#!/usr/bin/env python3
"""Fix ceec empty content: re-fetch all detail pages via browser."""
import asyncio, sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.browser import BrowserPool
from src.db.mongo import create_mongo_repository
from src.core.config import get_settings

SITE_ID = "中国能源建设集团有限公司_电子采购平台"
BATCH_SIZE = 3
SLEEP_BETWEEN = 2.0

async def fetch_detail(browser, url):
    """Fetch detail page content using browser"""
    async with browser.new_page() as page:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            text = await page.inner_text("body")
            html = await page.content()
            return text.strip(), html
        except Exception as e:
            print(f"  fetch error: {e}")
            return None, None

async def main():
    settings = get_settings()
    mongo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    
    # Find short-content notices
    col = mongo._collection
    cursor = col.find({
        "source_site_id": SITE_ID,
        "content_text": {"$exists": True},
        "$expr": {"$lt": [{"$strLenCP": "$content_text"}, 200]}
    })
    docs = await cursor.to_list(length=200)
    print(f"Found {len(docs)} short-content notices")
    
    pool = BrowserPool()
    await pool.start()
    browser = await pool.get()
    
    fixed = 0
    for i, doc in enumerate(docs):
        url = doc.get("detail_url") or doc.get("url")
        title = doc.get("title", "")[:70]
        old_len = len(doc.get("content_text", ""))
        print(f"[{i+1}/{len(docs)}] {title} old={old_len}", end=" ", flush=True)
        
        try:
            text, html = await fetch_detail(browser, url)
            if text and len(text) > old_len + 100:
                await col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"content_text": text, "content_html": html or ""}}
                )
                fixed += 1
                print(f"OK new_len={len(text)}")
            else:
                print(f"SKIP got {len(text) if text else 0}")
        except Exception as e:
            print(f"ERR {e}")
        
        if (i+1) % BATCH_SIZE == 0:
            await asyncio.sleep(SLEEP_BETWEEN)
    
    await pool.stop()
    print(f"Done: {fixed}/{len(docs)}")

asyncio.run(main())
