# AJAX API 发现技巧：使用 Performance API 找隐藏端点

一些招投标站点（如 cdt-ec.com 大唐电商平台）使用 layui 表格组件 + AJAX POST 动态加载数据，但页面源 HTML 中不直接暴露 API URL。通过浏览器 Performance API 可快速发现隐藏端点。

## 调试步骤

### 1. 页面加载后，立即检查 XHR/Fetch 请求

在 Hermes Browser 中导航到目标页面后，执行：

```javascript
performance.getEntriesByType('resource')
  .filter(r => r.initiatorType === 'xmlhttprequest' || r.initiatorType === 'fetch')
  .map(r => r.name)
```

这会返回所有 XHR 和 fetch 请求的 URL 列表，包含完整的 API 路径和参数。

### 2. 发现 API 后，在浏览器上下文中直接调用

利用浏览器已有的 Cookie/会话（绕过反爬），在 `browser_console(expression=...)` 中执行 fetch：

```javascript
fetch('https://www.example.com/notice/api/getList', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest'
    },
    credentials: 'include',
    body: 'page=1&limit=20&message_title=BIM&startDate=&endDate='
}).then(r => r.text()).then(t => { 
    document.body.innerHTML = '<pre>' + t.substring(0, 5000) + '</pre>'; 
})
```

### 3. 查看结果

在 fetch 完成后，用 `browser_snapshot(full=true)` 读取响应内容。

## 关键参数识别

从搜索表单的 input name 属性反推 API body 参数。常用字段映射：
- 公告名称 → message_title / title / keyword / keyWords
- 采购单位 → purchaser_company_name
- 采购单编号 → purchase_order_no / purchase_code
- 采购类型 → purchase_type
- 招标编号 → inviteno / tender_no
- 日期范围 → startDate, endDate

## 适用场景

- layui 表格（layui-table）异步加载数据的站点
- Vue/React SPA 站点（通过 Performance API 发现 REST 后端）
- 搜索表单提交后页面无刷新的 AJAX 搜索
- 有反爬/CSRF 保护（405）但浏览器会话可通过的站点

## 注意事项

- `credentials: 'include'` 是必需的 — 携带浏览器已有的 Cookie
- 部分站点需要 `X-Requested-With: XMLHttpRequest` 头来区分 AJAX 和普通 HTTP 请求
- 如果 API 返回 405，检查 Referer 或 Origin 头
- 如果 fetch 后浏览器跳转到空白页，说明搜索提交触发了页面重定向（非 AJAX 模式），该搜索不支持纯 API 方式
