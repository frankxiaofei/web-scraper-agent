# JSP/ASP.NET 站点 onclick 详情提取模式（华电 chdtp.com 案例）

## 模式识别

华电集团电子商务平台（chdtp.com）使用 **JSP/ASP.NET** 架构。其特征与 SPA 站点完全不同：

### 详情链接格式

```html
<a href="javascript:toGetContent('zhaobiaogg/2026/07/06/zhaobiaogg_3884554_38308.html')">
  贵州乌江塘寨分公司1、2号炉脱硝钢结构补强项目招标公告
</a>
```

详情 URL 通过提取 onclick/href 中的路径参数后拼接 `staticPage` 基路径得到：

```
https://www.chdtp.com/staticPage/{path}
```

### 通用模式

JSP 站点的详情链接通常使用以下格式之一：

| 格式 | 示例 | crawl_rules 配置 |
|------|------|-----------------|
| `href="javascript:funcName('{path}')"` | `toGetContent('xxx.html')` | `link_extractor.type: onclick` + `pattern: "toGetContent\\('([^']+)'\\)"` |
| `onclick="funcName('{path}')"` | `openDetail('xxx.html')` | 同上，`pattern: "openDetail\\('([^']+)'\\)"` |
| `href="/staticPage/{path}"` | `/staticPage/xxx.html` | 直接使用 `url_format`，无需 link_extractor |

### crawl_rules 配置

```yaml
detail:
  fetch_detail: true
  strategy: dom
  url_pattern: "https://www.chdtp.com/staticPage/.*"
  link_extractor:
    type: onclick
    pattern: "toGetContent\\('([^']+)'\\)"
  url_format: "https://www.chdtp.com/staticPage/{path}"
  content_selector: "table.LayoutTable td"
  wait_for: "table.LayoutTable"
```

注意 `link_extractor` 下的字段映射：url_format 中 `{}` 会被 `{path}` 替换（path 是 pattern 中捕获组提取的值）。

## 详情页内容验证（华电详情页不需要登录）

直接 `navigate` 到 `https://www.chdtp.com/staticPage/{path}` 即可查看完整内容，包括：

- 招标公告标题
- 招标编号
- 项目规模、地点、履约期限
- 招标范围
- 投标人资格要求（专用/通用）
- 招标文件获取方式
- 开标时间等

无需登录。这与 SPA 站点（如华能 ec.chng.com.cn）形成对比，后者详情需登录。

## 列表页访问（绕过 iframe）

主页面 `caigou.jsp?cgtype=4` 是一个框架页，实际列表内容在 `iframe#iframepage4` 中加载。可以直接访问：

```
https://www.chdtp.com/webs/queryWebZbgg.action?zbggType=1
```

这个 URL 直接返回完整的公告列表 HTML（含分页），无需经过框架页面。

### 列表列结构

| 列 | 选择器 | 内容 |
|----|--------|------|
| 公告状态 | `td.td_1` | 正在发布/停止发布 |
| 标题 | `td.td_2 a` | 公告标题 + onclick 详情链接 |
| 业务类型 | `td.td_3` | 服务/工程/货物 |
| 发布日期 | `td.td_4 span` | [2026-07-06] |

## 分页机制

分页通过 form POST 提交 `page.currentpage` 参数执行。翻页按钮是图片输入元素：

```html
<input type="image" src="…page-next.png" onclick="submit();">
```

crawl_rules 中通过 `input[src*='page-next.png']` 作为 next_button 选择器，模拟点击触发 form 提交。每次点击后页面刷新（非 AJAX/SPA）。

## 搜索注意事项

`caigou.jsp` 框架页面上的搜索框提交会触发父页面的 `submitDo()` 函数，该函数设置 iframe 的 `src` 为新的 `queryWebZbgg.action?key=1-1&search=BIM` URL。

搜索的完全 POST 到 `searchAction.action`：
- URL: `https://www.chdtp.com/webs/searchAction.action`
- Method: POST
- Body: `key=1-1&search=BIM`

如果 `searchAction.action` 搜索结果页的结构与 `queryWebZbgg.action` 不同，需要在 crawl_rules 中为搜索模式的列表配置独立的选择器。

## 搜索入口（caigou.jsp）字段

| 元素 | 选择器 | 说明 |
|------|--------|------|
| 公告类型下拉 | `select[name='cgtype']` | 招标公告(cgtype=4)、中标结果等 |
| 搜索输入框 | `input[type='text']` | 输入关键词 |
| 搜索按钮 | `.btn_b` 或 `input[type='submit'][value='搜索']` | 提交搜索 |

## 与其他站点的对比

| 属性 | chdtp.com（华电 JSP） | ec.chng.com.cn（华能 Vue SPA） |
|------|----------------------|-------------------------------|
| 架构 | JSP/ASP.NET form POST | Vue 3 + Ant Design SPA |
| 详情需要登录 | 否（staticPage 直接查看） | 是（Vue 弹窗） |
| 列表加载 | 同步（页面刷新） | 异步（AJAX 渲染到表格） |
| 分页 | form POST 图片按钮 | Ant Design pagination 组件 |
| 详情链接 | `href="javascript:func('path')"` 可解析 | 无 `<a>` 标签，需 data-row-key |
| 搜索 | form POST 到 searchAction | 页面内 fill + click 按钮 |
