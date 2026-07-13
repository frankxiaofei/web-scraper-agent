# dlzb_power Search-Entry Sync Failure & Fix

## 症状（已修复）

`dlzb_power` (电力招标网) 定时同步一致失败：

```
Page.wait_for_selector: Timeout 15000ms exceeded.
  - waiting for locator("#renderData, .search-list, .list-box, div.search-item") to be visible
   34 × locator resolved to hidden <textarea id="renderData">{"traceid":"...","lang...</textarea>
```

## 根因（双层问题）

### 问题 1：entry_url 是旧搜索页，关键词已过时

- 旧 `entry_url`: `https://www.dlzb.com/search/` + `keywords=智慧农业`
- 该页面的目的仅为展示搜索框和筛选器，**不渲染列表 DOM**
- `#renderData` 是 Destoon CMS 搜索页的隐藏 `<textarea>`，存放 AJAX 数据，**不可见**但 DOM 中存在

### 问题 2：`list_page.strategy: dom_after_ajax` 需要可见元素

`dom_after_ajax` 策略的 `wait_for` 使用 Playwright `wait_for_selector`，默认等待元素 **visible**。`#renderData` 是 `<textarea style="display:none">` 或其他隐藏方式，因此在 15s 内始终不可见 → 超时。

## 最终修复方案（2026-07-03）

### 1. 改 entry_url 为带关键词的搜索结果页

Destoon CMS 真实的搜索结果页 URL：
- 搜索页（搜索框界面）：`https://www.dlzb.com/search/` — 不渲染结果列表
- 搜索结果页（带关键词）：`https://www.dlzb.com/zb/search.php?kw=BIM` — 渲染结果列表即 `d-zb-` 链接

修改后的完整配置：

```yaml
entry_url: https://www.dlzb.com/zb/search.php?kw=BIM
list_page:
  strategy: dom                    # 从 dom_after_ajax 改为 dom
  wait_for: ".list_left"           # 用页面主体容器而非 #renderData
  container: ".list_left"
  item: "li"
  title: "a[href*='d-zb-']"
  link: "a[href*='d-zb-']"
  date: "span.fr, .pub-date, span.pub-date"
```

### 2. 同步更新 sites.yaml 的 url 字段

```yaml
- id: dlzb_power
  url: https://www.dlzb.com/zb/search.php?kw=BIM   # 改为带关键词的搜索结果URL
  enabled: true                                     # 启用
```

### 3. 验证结果

浏览器实测 `https://www.dlzb.com/zb/search.php?kw=BIM`：
- 标题：BIM招标公告-中国电力招标网
- 结果数：共1612条/71页
- 分页：`?page=N` 参数格式（如 `&page=2`）
- URL 模式：`https://www.dlzb.com/d-zb-{id}.html`
- 每条结果含发布日期（格式：2026-07-03）

## 经验教训：Destoon CMS 搜索页 ≠ 搜索结果页

dlzb.com 系站点使用 **Destoon CMS**。区分：
- 搜索页 URL: `/search/` — 只渲染搜索框 + 筛选条件
- 搜索结果页 URL: `/zb/search.php?kw=BIM` — 渲染结果列表
- `#renderData` hidden textarea 是 Destoon 搜索页的特征标志

当 crawl_rules 的 `wait_for` 超时时，首先确认 entry_url 是否为搜索结果页而非搜索页。用浏览器直接访问 entry_url，检查页面是否渲染了实际列表项。如果看到 `#renderData hidden textarea` 说明你进了搜索页而非搜索结果页。

## 2026-07-03 更新：即使搜索页 URL 正确也被 WAF 拦截

即使 entry_url 已正确设置为 `/zb/search.php?kw=BIM`，该页面仍然无法渲染。原因：`www.dlzb.com` 的 `/zb/` 路径部署了阿里云 WAF，对包括 curl 和 Playwright 在内的所有请求返回 WAF 验证页面，而非实际搜索结果。

**区分方法**：
- 旧问题（搜索页）：`curl -sL 'https://www.dlzb.com/zb/search.php?kw=BIM'` 正常返回 HTML 列表
- 新问题（WAF）：`curl -sL 'https://www.dlzb.com/zb/search.php?kw=BIM' | grep -c "renderData\|aliyun_waf"` > 0

详见 `references/dlzb-power-waf-blockade.md`。