# Next.js SPA Search: No Visible Results Pattern

## The Problem

Some Next.js-based SPA bid sites (notably `ceec.dnezb.com/3001` and its variants) have search functionality that produces **no visible DOM changes** when a keyword is entered and the search button is clicked. The page stays exactly the same — no new elements appear, no loading indicator, no error message.

## Observed Behavior at ceec.dnezb.com/3001

- URL: `https://ceec.dnezb.com/3001` (中国能源建设电子采购平台-招标公告专区)
- Framework: Next.js (client-side rendering via `/_next/static/chunks/`)
- Has a visible search input (placeholder: "请输入项目名称，多关键词空格隔开") and a "搜索" button
- Filling the input via WebBridge `fill()` and clicking via `click()` — or even via JS `nativeInputValueSetter` + `click()` — produces:
  - Same URL stays (no navigation to a search-results page)
  - Same DOM stays (no new elements, no results list)
  - `document.querySelectorAll('a[href*="d-zb-"]')` returns 0
- The URL parameter `?keyword=BIM` also does nothing — the page ignores it

## Likely Cause

The search is either:
1. An **AJAX call** to a hidden API endpoint that produces no render because the site requires login to see search results
2. A **proxy search** that redirects to the parent platform (dlzb.com) but is blocked or gets no response when the parent requires its own search flow
3. A **stub search UI** — the search field exists in the template but the backend integration was never completed or requires authentication cookies not present in the WebBridge session

Since `ceec.dnezb.com/3001` is a sub-branded portal of the dlzb.com (电力招标网) platform, the search likely proxies to dlzb.com's search. When the proxy fails silently, the SPA shows nothing.

## Diagnosis Steps

When you encounter a SPA site where search produces no visible results:

1. **Check the URL** — did it change? If not, the search is purely client-side or AJAX
2. **Check for loading spinners** — watch the page for 5-10 seconds after clicking search
3. **Use `skill_webbridge_evaluate`** to check:
   ```javascript
   // Did the AJAX request produce an error?
   // Check if there's a hidden results container
   document.querySelectorAll('[class*="result"], [class*="list"], [class*="search"]').length
   
   // Check the full body text for any keyword match
   document.body.innerText.includes('BIM')
   ```
4. **Check network requests** (via the user's browser DevTools F12) for any XHR/fetch to the parent domain
5. **Try the parent site directly** — navigate to `https://www.dlzb.com/search/?keywords=BIM` instead

## Next Steps When Search Fails

1. **Try the parent site directly** via WebBridge navigate
2. **If parent also fails** (page load timeout), fall back to:
   - `web_search(site:搜索平台 BIM)` for informational results
   - The site's scheduled sync (APScheduler) which may capture new items via its own crawl_rules
   - Report to user: "该站点的搜索功能无返回结果，可能是需要登录或后端集成未完成"

## SSR Data Extraction via `/_next/data/` Build-Specific JSON Endpoint

### Key Discovery

Next.js SSR sites expose their page-level data through a **build-specific JSON endpoint** at:

```
https://{domain}/_next/data/{buildId}/{path}.json?{queryParams}
```

This returns the full `pageProps` as JSON — **no browser needed**, just a simple `curl`. The `buildId` lives in the page HTML as `window.__NEXT_DATA__` (or hardcoded in `<script>` tag).

### ceec.dnezb.com Pattern

For ceec.dnezb.com (the ceec subdomain, NOT www.dnezb.com):

1. Get the `buildId` from the page source:
   ```bash
   curl -s https://ceec.dnezb.com/3001 | grep -oP '"buildId":"[^"]+"' | cut -d'"' -f4
   # Returns e.g. "e4a8a8e4bd9f4b1dbcfe2b82ca4d6263308964db"
   ```

2. Access the JSON endpoint for any page:
   ```bash
   # Page 1 (15 results, totalCount=3626)
   curl -s "https://ceec.dnezb.com/_next/data/{buildId}/ceec/3001.json?noticeCode=3001&page=1"

   # Page 2
   curl -s "https://ceec.dnezb.com/_next/data/{buildId}/ceec/3001.json?noticeCode=3001&page=2"
   ```

3. The `__NEXT_DATA__` in SSR also works via `skill_webbridge_evaluate`:
   ```javascript
   JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps
   ```

### Data Structure

```json
{
  "pageProps": {
    "uid": 0,
    "siteId": 242,
    "enSiteName": "ceec",
    "siteName": "中国能源建设电子采购平台",
    "shortName": "中国能建",
    "currentNoticeCode": 3001,
    "pageNum": "1",
    "noticeGroupName": "招标公告",
    "groupItems": [
      {"name": "招标公告", "noticeTypeCode": 3001, "sort": 1},
      {"name": "资格预审公告", "noticeTypeCode": 3003, "sort": 2},
      {"name": "候选人公告", "noticeTypeCode": 3019, "sort": 3},
      {"name": "中标公示", "noticeTypeCode": 3011, "sort": 4}
    ],
    "initialState": {
      "siteNoticeCodeArticles": {
        "data": {
          "result": 0,
          "articles": [
            {
              "articleId": 41188239,
              "title": "中能建定安县龙河镇大岭建筑用花岗岩矿项目二采区树木砍伐、清表和矿建道路修筑招标采购招标公告",
              "noticeTime": 1782662400000,  // epoch ms
              "noticeTypeCode": 3001,
              "provinceCode": 0,
              "cityCode": 0,
              "siteId": 242,
              "purchaserCompanyId": null,
              "purchaserCompanyName": null,
              "projectId": "PCID-GC-DAXMGS-DAXMGS-2026-001",
              "pinMuType": 2,
              "cutOffTime": 1783040445000   // epoch ms
            }
          ],
          "totalCount": 3626
        }
      }
    }
  }
}
```

### Detail URL Construction

The `articleId` maps to detail URLs at:
- `https://ceec.dnezb.com/detail/{articleId}` (ceec subdomain)
- `https://www.dnezb.com/detail/{articleId}` (parent domain)

For this site (siteId=242), detail URLs work at the parent domain `www.dnezb.com/detail/{articleId}`.

### Important: Data Only Contains Current Page's Category

The SSR JSON only contains data for the **current page** of the **current noticeCode category** (3001 = 招标公告). To get other categories:
- `/ceec/3003.json?noticeCode=3003` (资格预审公告)
- `/ceec/3019.json?noticeCode=3019` (候选人公告)
- `/ceec/3011.json?noticeCode=3011` (中标公示)

### General Application

This pattern works for **any Next.js SSR site** that uses `getServerSideProps` (indicated by `__N_SSP: true` in `__NEXT_DATA__`):

1. Find the site base URL and path structure
2. Extract `buildId` from any page
3. Construct `/_next/data/{buildId}/{path}.json` with the same query params
4. Parse the `pageProps` response JSON

**Limitations**: 
- Only works for SSR pages (not client-side rendered SPA routes)
- Only returns the page's own data scope (not search results)
- Some sites rotate buildId on deploy — it's stable between deployments

## Positive Case: ceec.dnezb.com (Main Domain) Search Works

**This session confirmed** that `ceec.dnezb.com` (NOT the `/3001` sub-path) has a fully working search. The `/3001` sub-path was a No-Op, but the main domain at `https://www.dnezb.com/search?kw=BIM&si=242` returns real BIM results.

### Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `kw` | `BIM` (or any keyword) | Search term |
| `si` | `242` | Site/channel filter. `242` = 能建招标 (中国能源建设集团) |
| `page` | `1`, `2`, `3`, `4` | Page number (MUI pagination, up to 4 pages for BIM) |

### Working Search URL

```
https://www.dnezb.com/search?kw=BIM&si=242
https://www.dnezb.com/search?kw=BIM&si=242&page=2
```

### Extraction Technique

The page is a Next.js SSR app (NOT a pure SPA). Data is rendered server-side and visible in the initial HTML.

```javascript
// Extract all detail links with titles and dates, then filter client-side
(() => {
  const links = document.querySelectorAll('a[href*="detail"]');
  const results = [];
  links.forEach(a => {
    const text = a.textContent.trim();
    const href = a.href;
    if (text && href && text.length > 3) {
      // Walk up DOM to find date and type
      let parent = a.parentElement;
      let dateText = '';
      let typeText = '';
      while (parent && parent.tagName !== 'BODY') {
        const t = parent.textContent.trim();
        const dateMatch = t.match(/20\d{2}-\d{2}-\d{2}/);
        const typeMatch = t.match(/(招标公告|询价采购|中标公示|采购结果|资格预审公告|招标预告|中标候选人公示)/);
        if (dateMatch && !dateText) dateText = dateMatch[0];
        if (typeMatch && !typeText) typeText = typeMatch[0];
        parent = parent.parentElement;
      }
      results.push({title: text, url: href, date: dateText, type: typeText});
    }
  });
  return results;
})()
```

### Detail Page Content Extraction via WebBridge evaluate

Despite being Next.js SSR, detail pages (`/detail/{articleId}`) render the full notice body text in the initial HTML at `www.dnezb.com`. **No API call needed.** Example:

```javascript
// Approach 1: Full page text (if only need plain text)
document.body.innerText.substring(0, 3000)

// Approach 2: Targeted content from main container  
document.querySelector('main')?.innerText.substring(0, 3000) || document.body.innerText.substring(0, 3000)
```

Important caveat: **`skill_fetch_detail_page` returns empty** for these pages because it uses Playwright which doesn't render Next.js SSR properly on first load. Always use WebBridge `skill_webbridge_evaluate` for detail content extraction on Next.js SSR sites.

### MUI Pagination

The paginator is MUI `<nav class="MuiPagination-root">`. Click next page via:

```
skill_webbridge_click(selector='a[aria-label="Go to page N"]')
// Where N = next page number, e.g. "Go to page 2"
```

Or navigate directly: `skill_webbridge_navigate(url='https://www.dnezb.com/search?kw=BIM&si=242&page=2')`

### BIM Results Observed

- 4 pages total of BIM keyword results on 能建频道 (si=242)
- Most are "（BIM 在正文中）" — the search highlights BIM mentions in the body, not just titles
- ~20 items per page (MUI default)
- Weekly volume: typically 0-2 new BIM-tagged items per week (this week only 1: SketchUpPro software)

### Recommended Workflow for Weekly Incremental BIM Collection

```
1. skill_webbridge_navigate(url="https://www.dnezb.com/search?kw=BIM&si=242")
2. Extract all detail links + dates via evaluate (above JS)
3. Filter for publish_date >= last_monday
4. For each matching item:
   a. skill_webbridge_navigate(detail_url) on same tab
   b. skill_webbridge_evaluate(code=body extractor) → get content text
   c. skill_save_notice(site_id="中国能源建设集团有限公司_电子采购平台", ...)
5. Check page 2, 3, 4 for any more recent items
6. skill_webbridge_close(close_session=true)
```

## Related Sites

| Site | URL | Search behavior |
|------|-----|----------------|
| 中国能建电子采购平台 (/3001 sub-path) | ceec.dnezb.com/3001 | No visible results from search |
| 中国能建电子采购平台 (main domain, si=242) | www.dnezb.com/search?kw=BIM&si=242 | **WORKS** — returns real BIM results, MUI pagination |
| 电力招标网(父站) | www.dlzb.com | SPA, loads but may timeout from WebBridge |

## False Positive: Non-Bid Internal Systems

Some URLs provided as bid sites are actually internal OA/corporate portals. During this session:

- `http://oa.hdec.com/systemcenter/theme/newecidi/index.jsp` → **华东勘测设计研究院有限公司综合管理信息系统** (internal OA)
  - Shows logged-in user name (员工：李晓飞)
  - Has menus like 党建网, 生产管理, 知识管理, 财务共享, 勘测设计系统
  - **Not a bid notice site** — no public bidding announcements
  - The URL came from the bid.powerchina.cn notice's contact email domain (qi_yf@hdec.com)

**Pattern recognition**: When a URL contains `/systemcenter/theme/`, `/oa/`, or `/portal/` paths and shows a corporate intranet login, it's likely an internal system, not a public bid platform. Explain to the user rather than trying to crawl.
