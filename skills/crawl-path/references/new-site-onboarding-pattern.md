# 新站 Onboarding 模式（四站案例）

## 会话摘要

用户要求补充4个站点的BIM爬取任务：
- 华能 ec.chng.com.cn — 已有规则 + cron（无需操作）
- 华电 chdtp.com — 已有规则（含cgtype=4入口）+ cron（无需操作）
- 大唐 cdt-ec.com — 已有规则 + cron（无需操作）
- 三峡 eps.ctg.com.cn — 全新接入

## 三峡站的细节

### 页面结构

- 列表页: `/cms/channel/1ywgg1/index.htm`
- 翻页: `?pageNo=N` 参数（共555页）
- 列表项: `<li name="li_name"><a href="/cms/channel/1ywgg1/{id}.htm" target="_blank"><span>标题</span><em>2026-07-06</em></a></li>`
- 详情页: `/cms/channel/1ywgg1/{id}.htm`
- 搜索框: `#inp-txt`，搜索按钮: `#btnSearch`
- 顶部搜索框搜索后在新页面展示结果

### crawl_rules 要点

```yaml
list_page:
  container: "#list1"       # 精确容器
  item: "li[name='li_name']"
  title: "a span"           # 取span内的纯文本，不带图标字符
  link: "a"
  date: "a em:last-child"   # 最后一个em才是日期
search:
  enabled: true
  type: webbridge_interactive
  steps:
    - action: fill
      selector: "#inp-txt"
      value: "BIM"
    - action: click
      selector: "#btnSearch"
```

### 选择器陷阱

1. **title 中包含图标** — `<a>` 元素内有 `<i class="iconfont"></i>` 图标。
   用 `a span`（而非 `a`）作为 title selector，避免图标字符混入标题。
2. **date 中多个 em** — `<a>` 内有 `<em>`（采购方式，可能为空）和 `<em>`（日期）。
   用 `a em:last-child` 精确匹配日期。

### sites.yaml 注册

添加到文件末尾（标准 enterprise 模板）：

```yaml
- id: 中国长江三峡集团有限公司_电子采购平台
  name: 中国三峡集团电子采购平台
  url: https://eps.ctg.com.cn/
  category: enterprise
  parent: 中国长江三峡集团有限公司
  adapter: generic
  enabled: true
  soe: true
  fetch_detail: true
  max_items: 100
  min_delay_seconds: 2
```

### cron 任务 prompt

```text
爬取三峡集团电子采购平台（eps.ctg.com.cn）搜索BIM的招标公告。
site_id: 中国长江三峡集团有限公司_电子采购平台
方式：WebBridge交互搜索BIM，或遍历列表页标题匹配
入口URL: /cms/channel/1ywgg1/index.htm，pageNo翻页
搜索框: #inp-txt，搜索按钮: #btnSearch
详情页: /cms/channel/1ywgg1/{id}.htm
```

### 验证

`crawl_test` 返回 10 条有效项，标题干净（不含图标），URL 完整可访问。
