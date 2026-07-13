# dlzb_power — Correct Selectors (2026-07-05)

## Background

The site `dlzb_power` (电力招标网, www.dlzb.com) had broken crawl_rules that failed to extract any items from the BIM search results page. The rules were using incorrect selectors that didn't match the actual DOM structure.

## Root Cause

The old crawl_rules used:
- `container: ".list_left"` — too broad, included breadcrumb nav, ad banners, search filters
- `item: "li"` — correct element type
- `title: "a[href*='d-zb-']"` — correct attribute pattern but wrong class specificity
- `date: "span.fr, .pub-date, span.pub-date"` — `span.fr` did NOT exist for dates
- `wait_for: ".list_left"` — resolved immediately but on wrong container

## Correct DOM Structure (confirmed via browser_console)

```
.list_left
  ├── .mbnav (breadcrumb)
  ├── .bg_guang (ad)
  ├── <form> (search form)
  └── .con_list              ← actual list container
        └── <li>             ← 20 items per page
              ├── a.gccon_title        ← title + link
              ├── .xgwd                ← keyword tags
              └── span.gc_date         ← date (YYYY-MM-DD)
```

## Correct Selectors

```yaml
list_page:
  container: ".list_left .con_list"
  item: "li"
  title: "a.gccon_title"
  link: "a.gccon_title"
  date: "span.gc_date"
```

## Additional Info

- **Pagination**: type=page_number, page_param=page, URL style=query
  - Base URL: `https://www.dlzb.com/zb/search.php?kw=BIM`
  - Page URL: `https://www.dlzb.com/zb/search.php?kw=BIM&page=N`
  - 81 total pages, 1614 total items (as of 2026-07-05)
- **Detail URL format**: `https://www.dlzb.com/d-zb-{id}.html`
- **Category tags**: Some items show "询价" (inquiry) text before the title — this is inside the `<li>` but outside the `a.gccon_title` link text
- **Transport**: browser (uses Playwright, NOT WebBridge — the current generic adapter WebBridge 30s timeout suggests this site is better served by server-side browser)

## Debugging Approach Used

1. `crawl_get_run_logs` — confirmed consistent timeout error: `waiting for locator("#renderData, .search-list, .list-box, div.search-item") to be visible` with `34 × locator resolved to hidden <textarea id="renderData">`
2. `crawl_get_rule` — reviewed current rule selectors
3. `browser_navigate` — directly opened the search results URL in Hermes browser (residential proxy not needed, though WAF was bypassed)
4. `browser_console(expression=...)` — queried:
   - `document.querySelector('.list_left')` — confirmed container exists
   - `document.querySelector('a[href*="d-zb-"]')` — confirmed link pattern works
   - `document.querySelector('.con_list li')` — found actual list items
   - `document.querySelector('.gc_date').textContent` — verified date selector
   - `document.querySelector('.con_list li a.gccon_title')` — verified title element structure
5. `crawl_generate_rule` — fed findings into AI rule generator
6. `crawl_test(max_pages=1)` — dry-run returned 20 items, confirming fix works
7. `crawl_save_rule` — saved corrected YAML

## Relationship to Destoon CMS Ref

This is distinct from the `references/dlzb-power-search-page-sync-failure.md` issue. That file covers the **case where entry_url pointed to a search form instead of results page**. This file covers the **case where entry_url is correct but DOM selectors within the results page are wrong**.
