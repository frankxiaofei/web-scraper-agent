# Vue SPA 搜索与分页模式（华能 ec.chng.com.cn 案例）

## 站点概况

华能电子商务平台（ec.chng.com.cn / channel/home/#/purchase?checked=3）是 Vue 3 + Ant Design 的 SPA，具有以下特征：

- 页面分两大独立表格区：**招标专栏**和**非招标专栏**
- 每个区各有自己的搜索框和「搜标题」「搜全文」两个按钮
- 表格使用 Ant Design `<Table>` 组件，通过 `.ant-table-row` 渲染
- 详情通过点击行触发 Vue Router 弹窗（无 `<a>` 标签）
- 分页通过 `.ant-pagination li` 页码元素进行前端路由切换

## Search 按钮定位（关键）

页面有5个 `<button.ant-btn.ant-btn-primary>`：

| 索引 | class | 文本 | 所属区域 |
|------|-------|------|---------|
| 0 | `login ant-btn ant-btn-primary` | 登 录 | 全局导航 |
| 1 | `btn ant-btn ant-btn-primary` | 搜标题 | 招标专栏 |
| 2 | `ant-btn ant-btn-primary` | **搜全文** | **招标专栏** |
| 3 | `btn ant-btn ant-btn-primary` | 搜标题 | 非招标专栏 |
| 4 | `ant-btn ant-btn-primary` | 搜全文 | 非招标专栏 |

BIM 搜索应使用 `nth-child(3)` 定位索引2的搜全文按钮（招标专栏）：

```yaml
search:
  steps:
    - action: fill
      selector: "input.ant-input"
      value: "BIM"
    - action: click
      selector: "button.ant-btn-primary:nth-child(3)"  # 招标专区搜全文
```

注意区分：`btn ant-btn ant-btn-primary`（搜标题）和纯 `ant-btn ant-btn-primary`（搜全文）—— 非招标区的搜全文是索引4（`nth-child(5)`），但BIM搜索只需要在招标区操作。

## 容器选择器（双表格问题）

默认 `.ant-table-tbody` 会同时匹配招标和非招标两个表格，导致重复项。应限定：

```yaml
list_page:
  container: ".ant-table-tbody:first-of-type"  # 仅招标区
```

## 详情获取（需登录）

点击 `.list-text`（行内标题 span）触发 Vue Router 弹窗展示详情。但是：

- **没有登录态**：点击无任何反应（无弹窗、无页面变化）
- **Vue Router `isTrusted` 检查**：即使用户已登录，合成 MouseEvent 也可能不触发路由跳转
- **data-row-key 属性**：行上有 `data-row-key` 包含公告 ID，可用于构造 URL

推荐策略：将详情标记为 `requires_login: true`，仅通过 WebBridge 在用户已登录浏览器中进行交互式爬取。

## 翻页

SPA 翻页通过点击 `.ant-pagination li` 页码元素完成，URL 不变。使用 `evaluate` 点击：

```javascript
(function() {
  var pages = document.querySelectorAll('.ant-pagination li');
  // pages[0] = prev, pages[last] = next
  var nextBtn = pages[pages.length - 2]; // 倒数第二个是"下一页"按钮
  if (!nextBtn.classList.contains('ant-pagination-disabled')) {
    nextBtn.click();
    return 'clicked next page';
  }
  return 'no more pages';
})()
```

## 数据重叠

SPA 分页切换时可能出现第 2+ 页混入第 1 页数据。`skill_save_notice` 的 URL/content_hash 去重可自动处理，无需额外逻辑。

## 搜索类型选择

华能等站点提供「搜标题」和「搜全文」两种搜索模式。对于 BIM 关键词搜索，应优先使用「搜全文」—— 因为 BIM 关键词可能出现在公告正文而非标题中（如项目描述中的 BIM 建模要求）。

排查方法：分别尝试搜标题和搜全文，对比结果数量。搜全文通常返回更多（或至少相同）条数。
