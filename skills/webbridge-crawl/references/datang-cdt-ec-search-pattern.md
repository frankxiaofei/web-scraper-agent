# 大唐集团电子商务平台 (cdt-ec.com) 搜索模式

## 站点信息

- Site ID: `中国大唐集团有限公司_大唐集团电子商务平台`
- URL: https://www.cdt-ec.com
- 框架: layui table（非 SPA，非 Next.js）
- WAF: 阿里云 WAF (acw_sc__v2)
- API: `https://www.cdt-ec.com/notice/moreController/getList` (POST, form-data)

## 搜索交互

### WebBridge 搜索流程（已验证通过）

1. `skill_webbridge_navigate(url="https://www.cdt-ec.com/notice/moreController/toMore?globleType=0", new_tab=true)`
2. 阅读 snapshot，找到搜索框 `@e25`（公告标题）和搜索按钮 `@e34`（搜 索）
3. `skill_webbridge_fill(selector="@e25", value="BIM")`
4. `skill_webbridge_click(selector="@e34")`
5. 等待 3-5 秒，layui table 异步加载搜索结果
6. 查看 snapshot — 表格显示「无数据」（如果确实没有匹配结果）

### 关键行为

- **WebBridge 下搜索是 AJAX 行为** — 点击搜索按钮后，layui 框架发起 AJAX POST 到 `getList`，表格内容刷新，不跳转页面
- **Playwright 下搜索会跳转** — 因为 headless 环境中缺少正确的 Cookie/Referer，表单的默认 submit 行为触发，跳转到无关页面（如 `/home/cwemeAppDownLoad.html`）
- **BIM 搜索结果为零** — 2026-07-06 通过 WebBridge 验证，大唐系统确实没有 BIM 相关的招标公告

## API 说明

- 端点: `POST https://www.cdt-ec.com/notice/moreController/getList`
- Content-Type: `application/x-www-form-urlencoded`
- Headers: `X-Requested-With: XMLHttpRequest` (layui 标准)
- 参数:
  - `page` - 页码
  - `limit` - 每页条数（默认20）
  - `messagetype` - 0=招标公告
  - `message_title` - 标题关键词
  - `startDate` / `endDate` - 日期范围
- 响应: `{"code":0,"msg":"","count":N,"data":[...]}`
- WAF: API 被阿里云 WAF 保护，直接 HTTP POST 会被 acw_sc__v2 Cookie 挑战拦截

## 详情页

- URL 模板: `https://www.cdt-ec.com/notice/moreController/moreall?id={id}`
- 策略: DOM（content_selector=body）
- 2026-07 实测：WAF 被触发时详情页也返回挑战页面

## 结论

- 大唐必须走 **WebBridge** 爬取（WAF 阻挡 API 直连）
- 搜索 BIM 关键词目前真实结果为 0 条
- 如果走 API 方式，必须先通过浏览器获取 valid acw_sc__v2 Cookie
- 定时任务中日间爬取可以考虑跳过此站（已知无 BIM 数据），或保留为低频检查
