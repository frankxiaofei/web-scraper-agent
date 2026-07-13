# Destoon CMS 站点抓取参考

## 背景

dlzb.com 旗下的所有子站（包括 tjbid.dlzb.com、zgjtjs.dlzb.com、zhfdc.dlzb.com 等）使用 Destoon CMS 框架。
搜索结果页和列表页结构统一，`full_page` 策略即可覆盖，但 `skill_webbridge_extract_list` 可能遗漏部分数据。

## 关键发现

### extract_list 不完全覆盖

`skill_webbridge_extract_list(hint="BIM")` 在 dlzb.com 搜索结果页上只返回了 18 条显式含 "BIM" 标题的公告，
但 JS 验证显示 `document.querySelectorAll('a[href*="d-zb-"]')` 能提取到 70+ 条链接（包括其他无关公告）。

**根因**：extract_list 按 hint 关键词过滤，只返回标题带 "BIM" 的行，但搜索结果页可能有其他非 BIM 公告混入 DOM。

### 推荐提取策略

```javascript
// 从 dlzb.com 搜索页提取所有公告链接（含标题和 URL）
JSON.stringify(Array.from(document.querySelectorAll('a[href*="d-zb-"]')).map(a => ({
    title: a.textContent.trim(),
    url: a.href
})))
```

执行方式：
```
skill_webbridge_evaluate(
    code="JSON.stringify(Array.from(document.querySelectorAll('a[href*=\"d-zb-\"]')).map(a => ({title: a.textContent.trim(), url: a.href})))",
    session_id=...
)
```

**优点**：一次性获取所有 `<a>` 标签，不被 extract_list 的 hint 过滤限制。
**缺点**：返回的 `title` 字段可能为空（destoon CMS 有 `display:none` 的 `<a>` 副本），需结合相邻元素提取。

### 详情页正文获取

`skill_fetch_detail_page` 对 dlzb.com 的 d-zb-* 详情页有效，正文含 AI 导读和详细内容。
约 20% 的详情页返回 `content_text=""`（可能是公告已过期或无正文权限），这些可以跳过保存。

## 分页参数

destoon CMS 搜索页分页 URL 格式：
- 第 1 页: `https://www.dlzb.com/search/?kw=BIM`
- 第 2 页: `https://www.dlzb.com/search/?kw=BIM&page=2`
- 第 N 页: `https://www.dlzb.com/search/?kw=BIM&page=N`

## 翻页判定

翻页按钮 selector: `.pages a:has-text('下一页')` 或直接构造 `page=N` URL。

## 动作顺序

1. `skill_webbridge_navigate(url=搜索页URL)` 打开页面（确保已登录——检查是否有用户名显示）
2. `skill_webbridge_evaluate(code=JSON提取脚本)` 全量提取列表
3. 过滤标题含 BIM 的项
4. 对每个新项调用 `skill_fetch_detail_page` 获取正文
5. 按 site_id="dlzb_power" 调用 `skill_save_notice` 保存
