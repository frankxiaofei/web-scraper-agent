# General-Web Site Crawling Examples

This file contains site-specific patterns discovered through WebBridge crawling of non-bid sites.

## 腾讯网 (news.qq.com) — 世界杯频道

### Key Patterns

| Feature | Detail |
|---------|--------|
| Site type | JS SPA (single-page app with dynamic content) |
| Navigation | Sidebar clicks DON'T trigger page load (no URL change) |
| Workaround | Navigate directly to channel URL |
| Article URLs | `https://news.qq.com/rain/a/{yyyymmdd}{id}` |
| Content loading | "加载更多" button (class `.load-more`) |
| Article extraction | `web_extract` works well on `/rain/a/` URLs |
| Tracking | URLs include `?adChannelId=quanyun` — strip before `web_extract` |

### Channel URLs

| Channel | URL |
|---------|-----|
| 世界杯 | `https://news.qq.com/ch/quanyun` |
| 要闻 | `https://news.qq.com/` |
| 体育 | `https://news.qq.com/ch/sports` |
| 首页 | `https://www.qq.com/` |

### Navigation Does NOT Work Via Click

On QQ news sidebar, clicking a nav item (e.g. @e8 for 世界杯) does NOT change the page URL or load new content. The sidebar uses JS to switch content but the accessibility tree doesn't reflect the change.

**Fix**: Directly `skill_webbridge_navigate(url=channel_url)` to the target channel.

### Load-More Pattern

QQ news uses a "加载更多" button (class `.load-more`) for incremental content loading, not URL-pagination.

```javascript
// Check if button exists and is visible
const btn = document.querySelector('.load-more');
const style = btn ? window.getComputedStyle(btn) : null;
// {display: 'block', visibility: 'visible'} = still has content
// {display: 'none'} = no more content

// Click it
btn?.click();
```

After clicking, wait ~1-2 seconds for new content to render. The total articles counter increments with each click until the button hides.

### Extracting Article Links

```javascript
// Get all article links (filters out duplicates, short labels, play counts)
document.querySelectorAll('a[href*="/rain/a/"]').forEach(a => {
  const text = a.textContent.trim().substring(0, 80);
  // Skip play-count labels like "42万|03:36" or "6万|01:15"
  if (/^\d+[万亿]/.test(text) || /^\d+:\d+/.test(text)) return;
  articles.push({ title: text.replace(/^热点精选/g, '').trim(), url: a.href });
});
```

### Article Content Extraction

After collecting article URLs, use `web_extract` (not WebBridge navigate) to get full article text. Strip tracking parameters:

```
https://news.qq.com/rain/a/20260626V096F700?adChannelId=quanyun
→ https://news.qq.com/rain/a/20260626V096F700
```

The `web_extract` tool handles these URLs well and returns structured markdown content.

## 新浪体育 (sports.sina.com.cn)

[TBD — add when crawled]

## 网易体育 (sports.163.com)

[TBD — add when crawled]
