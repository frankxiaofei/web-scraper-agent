# 三峡集团电子采购平台爬取笔记

## 站点概况

eps.ctg.com.cn — 中国长江三峡集团电子采购平台。无 SPA 框架，使用传统 JSP/HTML 渲染。搜索通过服务端查询实现。

## 核心信息

| 属性 | 值 |
|------|-----|
| site_id | 中国长江三峡集团有限公司_电子采购平台 |
| 列表页 | /cms/channel/1ywgg1/index.htm |
| 详情URL | /cms/channel/1ywgg1/{id}.htm |
| 每页 | 10条 |
| 总页数 | 555页（截至2026-07） |
| 详情需登录 | 否 |

## 列表页

- 容器：`#list1`
- 列表项：`li[name='li_name']`
- 标题：`a span`（实际 text 在 a 内有 span 包裹标题，但 a 的 textContent 包含前缀图标 ``）
- 详情链接：`a`（完整的详情 URL，如 `https://eps.ctg.com.cn/cms/channel/1ywgg1/240636631.htm`）
- 日期：`a em:last-child`（格式如 `2026-07-06`）
- 分页：`pageNo` URL 参数，如 `?pageNo=2`

## 详情页

直接通过导航到详情 URL 即可访问完整内容：

- 内容容器：`.article-content`
- 标题：`h1` 或 `h2`
- 可见内容：招标编号、项目概况、招标范围、投标人资格要求、招标文件获取方式等全部招标公告内容

## 搜索注意事项（2026-07-06 验证更新）

搜索功能已确认可用：

- 搜索框 ID：`#inp-txt`
- 搜索按钮 ID：`#btnSearch`
- 搜索后跳转到：`/cms/search.htm?kwd=BIM&channelIds=...`（非 `/search.jspx`，独立搜索结果页）
- 搜索结果列表结构与普通列表相同：`li[name='li_name']`（可被现有list_page规则解析）
- 搜索结果列表通过 `search.js` AJAX 动态加载（curl初始显示0条，浏览器渲染后显示实际数量）
- BIM 搜索 **正常返回结果**：58条，6页（2026-07-06数据）。之前的"返回全部公告"可能是当时搜索功能异常，当前已验证正常工作
- 分页参数：`<input type="hidden" id="pageNo" name="pageNo" value="0">`，pageNo=0 是初始值
- 搜索后日期格式：`<a><em>2026-07-06</em></a>`，与普通列表一致

**search 规则配置**：
```yaml
search:
  enabled: true
  type: webbridge_interactive
  steps:
    - action: navigate
      url: "https://eps.ctg.com.cn/cms/channel/1ywgg1/index.htm"
      label: "招标公告列表页"
    - action: fill
      selector: "#inp-txt"
      value: "BIM"
    - action: click
      selector: "#btnSearch"
      label: "点击搜索（跳转到/cms/search.htm）"
    - action: wait
      seconds: 3
```

不需要在search中单独配置list_page——搜索结果页复用普通列表的DOM解析规则。

## 对比参考

三峡站点属于"稳重型"招标平台——页面结构稳定、无需登录可看详情、无 WAF/反爬。适合定时全量爬取。
