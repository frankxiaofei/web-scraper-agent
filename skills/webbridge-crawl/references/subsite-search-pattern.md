# dlzb 子站搜索重定向模式

## 现象

dlzb 系子站（如 zgjtjs.dlzb.com, tjbid.dlzb.com 等）的站内搜索框会重定向到父站 www.dlzb.com 的搜索结果页面。

### 典型流程

1. navigate 到子站首页 `https://zgjtjs.dlzb.com/v1/`
2. 在搜索框输入 `BIM` 并点击搜索
3. 浏览器跳转到 `https://www.dlzb.com/search/?kw=BIM`
4. 从此页面提取的公告列表是整个 dlzb.com 的搜索结果，不局限于该子站

### 处理方式

- 这是 dlzb CMS 的设计行为，不是 bug
- 搜索结果页的公告是全局的，可以正常提取和保存
- 如果用户期望只抓取中交建平台的 BIM 公告，需要额外过滤（通过 site_id 或来源字段）
- 搜索结果页链接为 `d-zb-{id}.html` 格式，适合直接获取详情

## Next.js SPA 详情页（dnezb.com）

ceec.dnezb.com（和 www.dnezb.com）是 Next.js 服务端渲染的 SPA。

### 详情页内容获取

非付费会员的详情页 `https://www.dnezb.com/detail/{id}` 会隐藏正文内容，返回付费墙。

### 关键元数据提取

通过 `#__NEXT_DATA__` JSON 节点可以提取到：
- title：公告标题
- noticeTime：公告时间时间戳
- detailUrl：原始来源平台链接（如中国五矿 ec.minmetals.com.cn）
- infoSource：信息来源平台名
- groupName：所属集团
- summary（Buffer 数据）：可能含压缩的摘要文本
- unlockArticleLevels：解锁权限级别（[6,2] 表示付费会员）

### 推荐做法

对 dnezb.com 类站点：
1. 在搜索结果页提取 {title, url}
2. 尝试 skill_fetch_detail_page 获取详情
3. 如果返回 empty content，用 skill_save_tagged_document 保存为 tagged_document 替代 notice 入库，注明来源和基本信息
4. 不必强求 detail 内容——这些站点本身就是聚合站，详情在原始平台

### dlzb.com 详情页

dlzb.com 的详情页 `https://www.dlzb.com/d-zb-{id}.html` 对登录会员可见摘要内容（AI整理版），完整内容仍需付费。通过 skill_fetch_detail_page 可以获取到 AI 导读摘要和联系方式。

## ceec.dnezb.com 子站域搜索无结果

ceec.dnezb.com 虽然归 dnezb.com 平台，但使用独立域名。在此域名下用 `search?q=BIM` 搜索时：

- 页面加载成功(200)，显示筛选器 UI 但无结果列表
- extract_list 返回 0 条，evaluate 查 DOM 无搜索结果节点
- 无 JS 错误（搜索 API 调用未被触发）

**可能原因**：需要登录态 / 后端 API 未对接独立子站域 / 搜索仅面向内部用户。

**替代方案**：用父站 `www.dnezb.com/search?q=BIM` 搜索，可获得全站结果。
