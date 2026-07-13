---
name: crawl-rule-generation
description: "新爬虫默认：WebBridge DOM 探查（第一步）→ 生成 crawl_rules YAML → 试跑 → 启用调度"
version: 1.1.0
author: web_scraper
---

# Crawl Rule Generation — 自动配置采集规则

为 **generic** 适配器站点或 **占位 crawl_rules** 自动生成 / 修复 `config/crawl_rules/{site_id}.yaml`，试跑验证后启用 `sites.yaml`。

**第一步必做**：`skill_webbridge_check` → `skill_webbridge_navigate` → `skill_analyze_list_dom`（或 `get_html` + analyze）。WebBridge 不可用才回退 HTTP `crawl_generate_rule` 或 HITL。

## 何时触发

- 用户说「配置爬取规则」「生成 crawl_rules」「为这个站写规则」
- `crawl_get_rule` 显示无规则、占位规则（如 `container: body`）、或 `skill_plan_crawl_path` 无法规划路径
- 用户说「补充」「确认下规则是否充分」「补充搜索」—— 这是**验证/修复已有规则**场景
- `adapter: generic` 且 `schedule_eligible=false`

## 验证已有规则（重点新增场景）

当用户要求「确认规则是否充分」「补充搜索」「补爬取任务」时，已经存在 `crawl_rules`。此时：

### 首次操作：完整状态快照

在开始任何操作前，先获取全局状态概览：
1. `crawl_list_sites()` — 所有站点的状态、enabled、sync 进度
2. `cronjob(action='list')` — 所有定时任务列表，识别重复任务（同名/同schedule）
3. 对每个要处理的站点：`crawl_get_rule(site_id)` — 读取现有规则

报告结构示例：
- **重复任务检测**：列出同名或同一站点不同名的重复定时任务
- **站点规则摘要**：对每个站点列出 entry_url、list_page.strategy、search 配置、detail 配置
- **关键问题**：已知WAF、登录墙、search按钮行为异常等

1. **先用 `crawl_get_rule` 读取规则**，理解现有 selectors / API 配置 / search 配置
2. **用 WebBridge 导航到 entry_url**，验证列表是否能加载数据
3. **对 search 配置**（如果有 `type: webbridge_interactive` 或 `type: api`），在 WebBridge 中手动执行搜索步骤验证
4. **对 detail 配置**，点击一条公告确认详情页可渲染
5. 只修改有问题的部分，不要全量重写

### 典型问题：验证规则时的意外发现

在验证已有规则时，经常发现以下问题，这些是规则文件本身无法体现的运行时行为：

**0. 搜索后跳转到独立搜索结果页（三峡模式）**

三峡(eps.ctg.com.cn) 的搜索行为是：在首页输入 BIM → 点击搜索 → 页面跳转到 `/cms/search.htm?kwd=BIM&channelIds=...`（独立搜索结果页），不是原地 AJAX 过滤。

搜索结果页的特征：
- URL 从 `/cms/channel/1ywgg1/index.htm` 变为 `/cms/search.htm?kwd=BIM&channelIds=...`
- 列表项结构与普通列表页完全一致（同为 `li[name='li_name']` 结构）
- 搜索页也是 `pageNo` 参数分页
- 结果总数显示在页面上方（如"当前搜索到 58 条"）

**关键区别**：`/cms/search.htm` 页面初始 HTML 中结果列表为空——列表通过 `search.js` 的 AJAX 请求动态填充。所以在 curl 中看不到列表，但浏览器（含 WebBridge）中可见。

**search 规则设计要点**：
- `search.type: webbridge_interactive` — 必须在 WebBridge 中操作搜索
- 不需要在 search 中单独配置 list_page — 搜索后的页面使用与普通列表相同的 DOM 结构
- 详情 URL 格式：`/cms/channel/1ywgg1/{数字id}.htm`（与普通列表一致）
- 搜索框 selector：`#inp-txt`，搜索按钮 selector：`#btnSearch`
- 详情 content_selector：`.article-content`，HTTP 直接访问（200），不需要登录

**验证步骤**（Hermes Browser）：
```javascript
// 1. 导航到首页
browser_navigate('https://eps.ctg.com.cn/cms/channel/1ywgg1/index.htm')
// 2. 输入 BIM
browser_type('@e7', 'BIM')
// 3. 点搜索
browser_click('@e17')
// 4. 确认跳转后的 URL
browser_console(expression='window.location.href')
// → "https://eps.ctg.com.cn/cms/search.htm?kwd=BIM&channelIds=204%2C210%2C..."
// 5. 确认列表项数
browser_console(expression='document.querySelectorAll(\'li[name="li_name"]\').length')
// → 10（每页）
```

**1. 搜索按钮行为异常**
- 大唐(cdt-ec.com)：搜索按钮点击后不发起AJAX请求，而是跳转到首页/CMS下载页（form提交行为不符合预期）
- 结论：搜索必须走API方式（`search.type: api`），不能依赖页面表单交互
- 注意API有WAF保护（阿里云），需WebBridge浏览器环境调用

**2. 批量任务中存在重复定时任务**
- 华电chdtp有两个BIM爬取任务同时存在：`华电chdtp-BIM每日爬取` 和 `chdtp-daily-bim-sync`，都在每天4:00执行
- 华能ec.chng也有两个同时存在：`华能电子商务平台 BIM每日爬取` 和 `华能ec.chng-BIM每日爬取`，都在每天4:30执行
- 验证规则时应同时检查cronjob列表，输出重复任务提醒给用户确认删除

**3. 搜索行为导致页面跳转而非原地过滤**
- 三峡(eps.ctg.com.cn)：搜索按钮触发页面跳转到 `/search.jspx?q=BIM`，不是原地过滤
- 搜索结果列表结构与普通列表可能不同，需要单独配置search的list_page
- 详情URL格式：`https://eps.ctg.com.cn/cms/channel/1ywgg1/{数字id}.htm`

**4. SPA 搜索按钮点击可能跳转到无关页面**
- 大唐(toMore页面)：表单内的搜索按钮被点击后，浏览器实际跳转到 `/home/cwemeAppDownLoad.html`（一个用于App下载的介绍页），而不是发起AJAX搜索请求
- 诊断方法：搜索点击后检查 `browser_console(expression='window.location.href')` 确认页面是否发生了跳转
- 如果跳转，说明表单的默认提交行为未禁用，需要走API方式而非表单交互

### 关键技术点（从本次会话提炼）

**Layui Table 数据加载探查** — 适用于大唐(cdt-ec.com)等使用 layui table.render 的站点：
```
// 在 WebBridge 中执行 JS 提取 table 配置
scripts.forEach(s => {
  const text = s.textContent || '';
  if (text.includes('layui.use') && text.includes('table') && text.includes('table.render')) {
    // 从 script 标签提取 url、cols、where 等配置
  }
});
```
关键字段：`url` = API 地址, `where` = 请求参数, `cols` = 列定义（含 templet 函数名）

**详情 URL 构造探查** — 当列表项使用 templet 函数生成链接时：
```
// 提取 addLink 等模板函数，找 href 模式
function addLink(d) {
  // 一般有类似：href="/notice/moreController/moreall?id="+id
  // 或 href="/notice/moreController/xjdhtml?id="+id
}
```
区分不同 message_type 走不同详情路径（招标类 vs 非招标类）。

**API 列表页的 detail_url_template** — 对于 `list_page.strategy: api` 的站点，detail 配置应使用 `detail_url_template` 字段构建完整详情 URL，格式如 `https://domain.com/path/to/detail?id={link}`。

**SPA 搜索交互验证** — 适用于华能(ec.chng.com.cn)等 Ant Design Vue 站点：
- 在 WebBridge 中 fill 搜索词 → click 搜全文按钮 → wait → snapshot 确认结果列表变化
- 注意 SPA 可能通过路由跳转而不是 Ajax 刷新，需要观察 URL 变化
- 搜索按钮的选择器要精确（区分搜标题和搜全文）

**iframe 嵌套搜索验证** — 适用于华电(chdtp.com)等传统 iframe 站点：
- caigou.jsp 页面中搜索框在顶层，但结果加载在 iframe 中
- 对于这种复杂嵌套，API 搜索（`POST /searchAction.action`）比 WebBridge 交互更可靠

**搜索后页面跳转的处理** — 适用于三峡(eps.ctg.com.cn)等站点：
- 搜索按钮可能触发页面跳转（`/search.jspx?q=BIM`），不是原地过滤
- 此时 `search.type: webbridge_interactive` 的 navigate+fill+click 流程需要等跳转完成
- 如果搜索结果列表结构与普通列表页不同，需要单独配置 search 的 list_page

## 工具链

| 步骤 | Tool | 说明 |
|------|------|------|
| 解析 | `crawl_resolve_site` / `crawl_get_rule` | 确认 site_id 与现状 |
| 探查 | `skill_webbridge_*` + `skill_analyze_list_dom` | 打开列表页，输出 `page_hints` |
| 生成 | `crawl_generate_rule` / `crawl_generate_workflow` / `crawl_generate_script` | 传 `page_hints`；workflow 可在 `/crawl-rules/{site_id}/workflow` 画布微调；`save=true` 落盘 |
| 校验 | `crawl_validate_rule` | Pydantic schema |
| 试跑 | `crawl_test` | 1 页预览，不入库 |
| 启用 | `crawl_enable_site` | `sites.yaml` + 规则 `enabled: true` |
| 确认 | `crawl_schedule_status` | `schedule_eligible` 应为 true |

## SOP（标准流程）

**步骤 0（强制）— WebBridge DOM 探查**：

```
skill_webbridge_check
  → skill_webbridge_navigate(entry_url, new_tab=true)
  → [click/wait 进入列表页]
  → skill_analyze_list_dom(session_id=…)   # 产出 page_hints
```

WebBridge 不可用 → `crawl_notify_user` + HTTP 页面提示或 `crawl_request_user_input`。

```
crawl_resolve_site
  → skill_webbridge_check
  → skill_webbridge_navigate(entry_url)
  → skill_analyze_list_dom(session_id=…)   # 或 get_html + analyze
  → crawl_generate_workflow(page_hints=…)   # 或 crawl_generate_rule
  → 打开 /crawl-rules/{site_id}/workflow 微调节点
  → crawl_test(max_pages=1)
  → crawl_enable_site (试跑成功时)
  → crawl_schedule_status
```

### 一步式（探查已完成）

```
crawl_generate_script(site_id, page_hints=…, save=true, dry_run=true)
  → crawl_enable_site (dry_run_ok=true)
```

## page_hints 格式

`skill_analyze_list_dom` 返回的 `page_hints` 含：

- 页面标题、URL
- 推断的 `container` / `item` / `title` / `link` / `date`
- 链接样本、疑似 API 路径

生成时 **原样传入** `crawl_generate_rule(page_hints=…)`，优于仅靠 HTTP 自动抓取。

## 试跑判定

`crawl_test` 成功条件：

- `success: true`
- `items` ≥ 1，且 title/url 像招标公告（非导航/footer 链接）

失败时：修正 selectors → 重新 generate → save → test。**不要**在未试跑通过时 `crawl_enable_site`。

## 调度资格

`generic` 站满足 `_generic_has_crawl_rules`（有效 `list_page`）后：

- `crawl_schedule_status.schedule_eligible = true`
- per-site interval 调度可纳入该站（仍受 `enabled` 与 crawl_scope 约束）

## 示例对话

- 「为 zcygov_national 自动生成爬取规则并试跑」
- 「接入 https://bidnews.cn 并配置 crawl_rules」
- 「这个占位规则不对，用 WebBridge 重新分析列表页生成规则」

## 限制

| 场景 | 处理 |
|------|------|
| 登录墙 / 验证码 | `crawl_request_user_input`，用户提供 cookie 或手动完成验证后继续 WebBridge |
| SPA / XHR 列表 | `page_hints` 中 api_hints → `list_page.strategy=api` 或 `dom_after_ajax` |
| WebBridge 超时 | 回退 HTTP `crawl_generate_rule`；或 Hermes browser 探查后传 `page_hints` |
| 试跑 0 条 | 选择器过宽/过窄，迭代修正；勿启用站点 |
| 企业堡垒机 + 滑块验证码（国家电投 ebid.espic.com.cn） | 主页面 (`bulletinListNew.html`) 为导航外壳，列表在 iframe `demo2.html` 中。iframe URL 含 slidercaptcha 组件（拼图滑块 "向右滑动填充拼图"），需用户手动拖动滑块。部署 Tingyun RUM 监控。不部署定时任务，仅 WebBridge + 用户手动操作。 |

## REST API（等价）

- `POST /api/crawl-rules/generate` — 生成 YAML
- `GET/PUT /api/crawl-rules/{site_id}/workflow` — 工作流画布读写
- `POST /api/crawl-rules/{site_id}/workflow/generate` — AI 生成工作流
- `POST /api/crawl-rules/save` — 保存
- `POST /api/crawl-rules/dry-run` — 试跑（`crawl_test` 底层）
- `POST /api/crawl-scripts/generate` — 一步 generate+save+dry_run
