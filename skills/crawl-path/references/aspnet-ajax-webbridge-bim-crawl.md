# ASP.NET WebForm AJAX 站 WebBridge BIM 爬取

## 站点特征

中国能建电子采购平台 `ec.ceec.net.cn`：
- ASP.NET WebForm 架构，使用 AJAX 异步加载（`CeecBidWeb.HomeInfo.ProjectList.getdata()` / `ProjectList_Search.getdata()`）
- **两个主要入口**：
  - BIM 搜索页 `/HomeInfo/ProjectList_Search.aspx?keyWords=BIM` — 通过 layui laypage 分页，BIM 结果极少（1条）
  - **服务类招标公告列表** `/HomeInfo/ProjectList.aspx?InfoLevel=MQA=&bigType=WgBCAEcARwA=` — 招采公告 > 招标公告页面，含货物/工程/服务三类，共54条/3页，每页20条（2026-07-06数据）。URL 参数 bigType 为 Base64 编码（`WgBCAEcARwA=` = "招标公告"）
- **多层分类结构**：`InfoLevel=MQA=`（一级：招采公告），`bigType=WgBCAEcARwA=`（二级：招标公告）。招标公告下再分子分类：货物/工程/服务（通过左侧导航 JS 切换，sid 参数：`aAB3AA==`=货物, `ZwBjAA==`=工程, `ZgB3AA==`=服务）
- 列表中的标题以 `[货物类]` `[工程类]` `[服务类]` 标签开头
- **详情页 URL 格式**：`/HomeInfo/ZhaoBiaoGG_Details.aspx?zbxmbh={hash}`（注意：是 `ZhaoBiaoGG_Details.aspx` 不是 `ProjectDetail.aspx`）
- 详情页内容直接在 body 中渲染为 `StaticText`（ASP.NET Label 输出），无 `#content` / `.detail-content` 等标准容器

## 站点接入完整步骤

### 1. 注册站点到 sites.yaml

在 `config/sites.yaml` 末尾添加：

```yaml
- id: 中国能源建设集团_ec_ceec
  name: 中国能建电子采购平台（ec.ceec.net.cn）
  url: https://ec.ceec.net.cn/
  category: enterprise
  region: null
  parent: 中国能源建设集团有限公司
  adapter: generic
  enabled: true
  mvp: false
  soe: true
  fetch_detail: true
  max_items: 50
  min_delay_seconds: 180
  notes: 能建主平台 ASP.NET AJAX; BIM 搜索走搜索页 AJAX
```

注意：YAML 中冒号 `:` 后必须加空格，URL 中带冒号的需要避开（不要写在 notes 的 URL 中）。

### 2. 添加入口别名（可选）

在 `src/core/site_aliases.py` 中添加：

```python
"ceec_ec": "中国能源建设集团_ec_ceec",
"ecceec": "中国能源建设集团_ec_ceec",
```

### 3. 生成并保存 crawl_rules

重点：
- `list_page.strategy: dom`（ASP.NET AJAX 页面普通 HTTP 无法获取数据，需 WebBridge/Playwright 浏览器渲染）
- 列表容器：`table`（layui 渲染的表格）
- 分页：`next_button` selector `a.layui-laypage-next`，disabled `a.layui-laypage-next.layui-disabled`
- 详情 content_selector：`body`（因为 ASP.NET Label 直接在 body 中渲染 StaticText，无标准容器）

核心配置：

```yaml
version: 1
site_id: 中国能源建设集团_ec_ceec
name: 中国能建电子采购平台（ec.ceec.net.cn）
enabled: true
entry_url: https://ec.ceec.net.cn/HomeInfo/ProjectList.aspx?InfoLevel=MQA=&bigType=WgBCAEcARwA=
list_page:
  strategy: dom
  container: "table"
  item: "tr"
  title: "td a"
  link: "td a"
  date: "td:last-child"
  wait_for: "table a"
pagination:
  type: next_button
  selector: "a.layui-laypage-next"
  disabled_selector: "a.layui-laypage-next.layui-disabled"
  wait_after_ms: 2000
detail:
  fetch_detail: true
  strategy: dom
  url_pattern: "/HomeInfo/ZhaoBiaoGG_Details.aspx"
  content_selector: "body"
  wait_for: "body"
limits:
  max_pages: 5
  max_items: 100
  max_depth: 2
  rate_limit_seconds: 3.0
```

### 4. 试跑验证

```sh
crawl_test(site_id="中国能源建设集团_ec_ceec", max_pages=1)
```

应返回 items 含 title + url。

### 5. 手动入库（首次一次性）

对于只有 1 条 BIM 结果的站点，首次爬取需要手动走完整链路：

```
skill_webbridge_navigate(entry_url)
skill_fetch_list_page(page_num=1)
skill_fetch_detail_page(url, title)  # 内容在 body 中
skill_save_notice(site_id, title, url, publish_date, content_text)
```

### 6. 建立定时任务

```yaml
schedule: "0 4 * * *"  # 每天凌晨4点
prompt: "执行 ec.ceec.net.cn BIM 爬取任务..."
enabled_toolsets: ["crawl", "crawl-skills"]
```

## 关键陷阱

### 陷阱 1: ASP.NET AJAX API 不能通过 HTTP 直接调用

`CeecBidWeb.HomeInfo.ProjectList_Search.getdata()` 是 ASP.NET AJAX 的 Page Method 调用，依赖 `__VIEWSTATE`、`__EVENTVALIDATION` 等隐藏字段，以及 ASP.NET AJAX 的 ScriptManager 基础设施。普通 HTTP POST 无法成功调用。

正确做法：使用 WebBridge navigate 到搜索页，让页面加载完整的 ASP.NET 视图状态，然后通过 `skill_webbridge_evaluate` 执行 JavaScript 直接调用：
```javascript
CeecBidWeb.HomeInfo.ProjectList_Search.getdata('BIM', 1, 50)
```

或者配置 `list_page.strategy: dom`，让 Playwright/WebBridge 浏览器渲染后再提取 DOM。

### 陷阱 2: 详情页 content_selector 不匹配

ASP.NET 详情页的内容通过 `<asp:Label>` 直接输出，在 DOM 中没有 `.content`、`#content`、`.detail-content` 等标准容器。默认选择器匹配不到内容，`skill_fetch_detail_page` 返回空。

修复：将 `content_selector` 设为 `"body"`，获取整个页面的文本内容。

### 陷阱 3: 搜索 BIM 结果极少

`ec.ceec.net.cn` 的搜索结果只搜索标题，不搜索正文。BIM 关键词在标题中出现的高招公告数量非常少（1条）。这不是爬取配置问题，而是平台数据本身有限。

### 陷阱 4: YAML 中冒号冲突

YAML 中写 `url: https://ec.ceec.net.cn/HomeInfo/ProjectList_Search.aspx?keyWords=BIM` 会报 YAMLError，因为 URL 参数包含冒号。用引号包裹或避免在 notes 字段中包含 URL 中的冒号。

## 搜索 API 提取（WebBridge evaluate 模式）

当需要直接通过 AJAX API 获取数据（不走 DOM 解析）时：

```javascript
// 搜索 BIM 关键词
let data = CeecBidWeb.HomeInfo.ProjectList_Search.getdata('BIM', 1, 50).value;
let parsed = JSON.parse(data);
let items = parsed.maindata[0];

// items 字段
// MsgTitle -- 标题
// PublishDate -- 发布日期 (格式: "2024/11/7 0:00:00")
// sys_id -- UUID，构造详情 URL: /HomeInfo/ProjectDetail.aspx?threadID={sys_id}
// MsgType -- 公告类型（采购公告/招标公告）
// ZhaoBiaoXMBH -- 项目编号
```

### 详情页内容提取（WebBridge evaluate 模式）

```javascript
// 从详情页提取结构化信息
let allText = document.body.innerText;

// 关键字段提取
const info = {
  projectNo: allText.match(/项目编号：([^\\n]+)/)?.[1]?.trim(),
  publishDate: allText.match(/公告发布时间：([^\\n]+)/)?.[1]?.trim(),
  projectName: allText.match(/项目名称：([^\\n]+)/)?.[1]?.trim(),
  bidDeadline: allText.match(/截标\\/开标时间：([^\\n]+)/)?.[1]?.trim(),
  projectType: allText.match(/项目类型：([^\\n]+)/)?.[1]?.trim(),
  bidder: allText.match(/招标人：([^\\n]+)/)?.[1]?.trim(),
};

// 公告正文
let contentStart = allText.indexOf('公告内容');
let bodyText = contentStart >= 0 ? allText.substring(contentStart + 4).trim() : '';
```

## 服务类招标公告列表作为 BIM 爬取入口（新增策略 2026-07-06）

与只返回 1 条结果的 BIM 搜索页不同，**服务类招标公告列表页**（`bigType=WgBCAEcARwA=`）包含全量招采公告（54条/3页）。其中的 `[服务类]` 条目可通过下游 BIM 关键词/LLM 分类管道进行过滤识别，无需依赖站内搜索功能。

**操作步骤**：

1. 修改 crawl_rules 的 entry_url 为用户给的列表 URL
2. 确认 `detail.url_pattern` 正确指向 `ZhaoBiaoGG_Details.aspx`（不是 `ProjectDetail.aspx`）
3. 分页策略：layui `next_button`（`a.layui-laypage-next`），与 BIM 搜索页一致
4. 选择器：`table > tr > td a`（标题）、`td:last-child`（日期）
5. 定时任务中声明 BIM 关键词（BIM、建筑信息模型、Revit 等）进行内容过滤

**完整 crawl_rules 配置**：
```yaml
version: 1
site_id: 中国能源建设集团_ec_ceec
name: 中国能建电子采购平台（ec.ceec.net.cn）
enabled: true
entry_url: https://ec.ceec.net.cn/HomeInfo/ProjectList.aspx?InfoLevel=MQA=&bigType=WgBCAEcARwA=
list_page:
  strategy: dom
  container: "table"
  item: "tr"
  title: "td a"
  link: "td a"
  date: "td:last-child"
  wait_for: "table a"
pagination:
  type: next_button
  selector: "a.layui-laypage-next"
  disabled_selector: "a.layui-laypage-next.layui-disabled"
  wait_after_ms: 2000
detail:
  fetch_detail: true
  strategy: dom
  url_pattern: "/HomeInfo/ZhaoBiaoGG_Details.aspx"
  content_selector: "body"
  wait_for: "body"
limits:
  max_pages: 5
  max_items: 100
  max_depth: 2
  rate_limit_seconds: 3.0
```

**定时任务 prompt 关键语句**：
```
爬取服务类招标公告列表，通过标题关键词（BIM、建筑信息模型、Revit、Navisworks、三维设计、数字化交付、数字孪生、施工模拟、碰撞检测）过滤后入库
```

## 新增站点·国能e招（chnenergybidding.com.cn）

**站点特征**：
- 国家能源集团招标采购平台（原神华招标网）
- 静态 HTML 页面（非 SPA），URL 编码清晰
- **三级 URL 编码体系**：

| 层级 | 编码 | 含义 |
|------|------|------|
| 一级：公告类型 | `/bidweb/001/` | 公告信息模块 |
| 二级：公告类型细分 | `/001001/` = 资格预审, `/001002/` = **招标公告**, `/001003/` = 非招标公告, `/001004/` = 变更公告, `/001005/` = **候选人公示**, `/001006/` = **中标公告**, `/001007/` = 终止公告, `/001009/` = 招标计划, `/001010/` = 招标文件公示 |
| 三级：子分类 | `001002001` = **货物**, `001002002` = **工程**, `001002003` = **服务** |

**关键发现：所有公告类型共享受三级子分类结构**。每个公告类型页（招标公告、候选人公示、中标公告等）都有货物/工程/服务三个子分类标签。

- 服务类招标公告入口：`/bidweb/001/001002/001002003/moreinfo.html`
- 列表项：编号（CEZB260105541）+ 标题 + 发布日期
- 详情页格式：`/bidweb/001/001002/001002003/20260706/{uuid}.html`
- 页面通过 JS 切换分类（`var category = document.location.href.split("/")[2]`），所有三个子分类链接共享同一 `moreinfo.html` URL，但实际指向不同 path

**分页特征**：query 参数分页（`?page=N`），非路径式分页
- 第1页：`moreinfo.html`
- 第2页：`moreinfo.html?page=2`
- 列表上的分页器使用页面跳转表单输入页码
- 注意：skill_fetch_list_page 翻页时 `next_page_url` 字段可能返回另一个分类的 URL（bug），依赖 `page_param` 参数构造正确的翻页 URL

**crawl_rules 配置（覆盖全部三个招标公告子分类）**：

使用 `entry_steps` 覆盖货物(001002001)、工程(001002002)、服务(001002003)三个子分类。每个 step 是一个独立的 navigate 动作，规则执行器会依次处理每个入口的列表。

```yaml
version: 1
site_id: chnenergybidding_national
name: 国能e招（国家能源集团招标网）
enabled: true
entry_url: https://www.chnenergybidding.com.cn/bidweb/001/001002/001002001/moreinfo.html
entry_steps:
  - action: navigate
    url: https://www.chnenergybidding.com.cn/bidweb/001/001002/001002001/moreinfo.html
    label: 招标公告-货物
  - action: navigate
    url: https://www.chnenergybidding.com.cn/bidweb/001/001002/001002002/moreinfo.html
    label: 招标公告-工程
  - action: navigate
    url: https://www.chnenergybidding.com.cn/bidweb/001/001002/001002003/moreinfo.html
    label: 招标公告-服务
list_page:
  strategy: dom
  container: div.right-bd
  item: ul.right-items li.right-item
  title: a.infolink
  link: a.infolink
  date: span.r
pagination:
  type: page_number
  page_param: page
  page_size: 15
  max_pages: 100
  stop_when_empty: true
detail:
  fetch_detail: true
  strategy: dom
  url_pattern: "/bidweb/001/001002/"
  content_selector: div.right-bd
limits:
  max_pages: 100
  max_items: 1500
  max_depth: 2
  rate_limit_seconds: 1.0
```
