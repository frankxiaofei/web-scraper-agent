# 中央政府采购网 (zycg_national) 爬取诊断

site_id: `zycg_national`
entry_url: `https://www.zycg.gov.cn/`

## 历史错误

2026-07-04 及之前的运行失败原因是：`entry_steps` 中的 `click_selector` 指向 `div.topNav > div > ul.navBox > li:nth-of-type(3) > a`，但实际导航栏 CSS class 是 `TopMarginLeft left_margin`，且 `nth-of-type(3)` 不适用于该页面结构（导航项之间有其他元素隔开）。

## 修复与现状

**修复**：2026-07-05 将 `entry_url` 改为直接导航到采购公告列表页：
- `https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html`
- 删除 `entry_steps`，改为直接 navigate
- 列表容器改为 `#TabContent .tab-pane.active ul#noticeShow`

**现状**：`crawl_trigger` 运行仍返回 0 条。原因待排查——可能列表内容通过 JS 异步加载（`listLeft` 侧栏分类选择触发 AJAX 加载）。

## 实际 DOM 结构（WebBridge 2026-07-05 确认）

- 导航栏「采购公告」链接：`/freecms/site/zygjjgzfcgzx/cggg/index.html`
- 公告列表容器：`#TabContent > .tab-pane.active > ul#noticeShow`
- 列表项：`<li style="line-height:30px;height:30px;">`
- 公告链接：`<a href="/freecms/site/zygjjgzfcgzx/ggxx/info/2026/{uuid}.html?id={id}" target="_blank" class="titleHiding">`
- 标题在 `<span>` 内
- 时间在 li 文本中（附在标题后）
- 分页：`button.turnPage.next-page`（点击触发 AJAX 刷新 #TabContent）
- 侧栏分类筛选：`<ul class="dropdown-menu1">` 含「全部」「单独委托项目」「批量集采」「电子卖场」等

## 可选的替代抓取方案

### 方案 A：WebBridge 手动提取

```javascript
// 提取列表所有公告
JSON.stringify(Array.from(document.querySelectorAll('#noticeShow li')).map(li => {
    const a = li.querySelector('a');
    return {
        title: a ? a.innerText.trim() : '',
        url: a ? 'https://www.zycg.gov.cn' + a.getAttribute('href') : '',
        date: (li.innerText.match(/(\d{4}-\d{2}-\d{2})/) || [''])[0]
    };
}))
```

### 方案 B：搜索 API

采购公告搜索页 `/freecms/site/zygjjgzfcgzx/cggg/index.html` 的搜索功能可能通过后端 API，需通过浏览器 Network 面板捕获请求。

## 结论

zycg 站非标准招标词（如 BIM）公告数量极少甚至为 0。该站主要发布政府集中采购（办公设备、IT 服务等），与 BIM 相关性很低。建议从巡检清单中降低优先级。
