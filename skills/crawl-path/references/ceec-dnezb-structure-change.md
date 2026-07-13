# ceec.dnezb.com 站点结构变更记录

site_id: `中国能源建设集团有限公司_电子采购平台`
主域名：`ceec.dnezb.com`（中国能建子站，`www.dnezb.com` 的 Next.js 子站平台）

## 已知结构变更

### 2026-07-05: `/ceec/3001.html` 返回 404

**变更内容**：`/ceec/{noticeCode}.html` 路径不再有效。之前使用的分类路径（noticeCode=3001 招标公告）返回 404。

**新入口**：能建招标专区搜索页 `https://ceec.dnezb.com/search?si=242`（si=242 为能建招标频道 ID）。

**首页无 SEO 路径**：直接访问 `https://ceec.dnezb.com/` 首页正常显示「招标公告」「中标公示」「采购公告」「中选公示」等分类导航（`@e5`-`@e9`），点击后只是首页局部切换，不走独立 URL。

**Next.js data API 可能已变更**：之前可用的 `/_next/data/{buildId}/ceec/3001.json` 路径也返回 404（原因同上 — noticeCode 路径不再存在）。

### 实际上这个站是 www.dnezb.com 的子站

公告详情页 URL 是 `https://www.dnezb.com/detail/{articleId}`（主站域名，无 ceec 子域名）。搜索后跳转到 `https://ceec.dnezb.com/search?si=242`（本子站域名）。详情页走主站域名。

## 当前有效的抓取路径

### 首页公告列表（最近若干条）

`https://ceec.dnezb.com/` 首页的 `<main>` 区域通过 SSR 渲染了一个公告表格：
- `<table><thead><tr><th>公告标题</th><th>发布时间</th></tr></thead>`
- 每行 `<tr>` 包含 `<td><a href="https://www.dnezb.com/detail/{id}">{title}</a></td>` + `<td>{date}</td>`
- **问题**：首页只展示最近 6 条左右，不是完整列表

### 能建搜索页（完整列表）

`https://ceec.dnezb.com/search?si=242`
- URL 参数：`si=242` 限定能建频道
- 渲染结果：包含 `<a href="/detail/{id}">` 的列表
- 需要进一步查看分页方式（MUI Pagination 或 turnPage buttons）

### WebBridge 提取 JS

```javascript
// 提取详情链接
JSON.stringify(Array.from(document.querySelectorAll('a[href*="/detail/"]')).map(a => ({
    title: a.innerText.trim(),
    url: a.href.startsWith('http') ? a.href : 'https://ceec.dnezb.com' + a.getAttribute('href'),
    date: ''  // 日期需要从父元素找
})));
```

## BIM 相关

该站 ceec.dnezb.com 是能建电子采购平台。BIM 公告通过能建频道搜索 `q=BIM` 可获取（2026-07 约 77 条）。BIM 搜索结果在 `www.dnezb.com` 主站搜索时覆盖全平台，不仅仅是能建频道。

## 结论

该站是电力能源招标网（dlzb.com/dnezb.com 系）的子站，Next.js SSR + MUI。crawl_rules 限 `max_pages: 10` 和 `max_items: 100`。如需完整爬取，首选 **WebBridge 搜索页提取** 或 **www.dnezb.com 主站搜索提取**。
