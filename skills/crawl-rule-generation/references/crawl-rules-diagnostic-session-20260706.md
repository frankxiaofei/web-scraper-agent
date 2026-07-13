# 电商平台 crawl_rules 诊断记录 (2026-07-06)

## 大唐集团 (cdt-ec.com)

**入口变更**: `more.jsp` → `toMore?globleType=0`（toMore 使用 layui table.render 异步加载）

**API 确认**:
- 接口: `POST /notice/moreController/getList`
- 参数: `page`, `limit`, `messagetype=0`, `message_title`, `startDate`, `endDate` ...
- 响应: `{code: "0", count: N, data: [...]}`
- data 中每项: `id`, `message_title`, `publish_time`, `message_type`, `purchase_type`, `deadline`, `message_no`, ...

**详情 URL 构造**（从 addLink 模板函数分析）:
- 招标类（message_type=0,1,2,3,21,22,25）: `/notice/moreController/moreall?id={id}`
- 非招标类（message_type=4,5,23,24,26）: `/notice/moreController/xjdhtml?id={id}`
- 全路径: `https://www.cdt-ec.com/notice/moreController/moreall?id={id}`

**搜索**: API body_override 增加 `messagetype=0`

---

## 华能集团 (ec.chng.com.cn)

**页面**: Vue SPA + Ant Design，`#/purchase?checked=3` 路由

**WebBridge 元素**:
- @e3: 搜索输入框 (`input.ant-input`)
- @e4: 搜标题按钮
- @e5: 搜全文按钮（第三个button）
- 列表行: `.ant-table-row`

**搜索验证**: 输入BIM后点搜全文，返回BIM相关招标公告（如"海上风电场升压站BIM建模"）

**详情**: 弹窗模式，需登录。`click_selector: ".ant-table-row td:first-child .list-text"`

**WAF**: 有强WAF防护，必须WebBridge

---

## 华电集团 (chdtp.com)

**入口**: `queryWebZbgg.action?zbggType=1`（直接POST访问，绕过iframe）
**备选入口**: `caigou.jsp?cgtype=4`（iframe嵌套，搜索框在顶层但结果在iframe中）

**搜索API**: `POST /webs/searchAction.action`，body `key=1-1&search=BIM`
- caigou.jsp 的搜索按钮点击后通过POST刷新iframe内容
- iframe 有7个，结构复杂
- WebBridge交互搜索不如API方式可靠

**详情**: `toGetContent('路径')` → `https://www.chdtp.com/staticPage/{路径}`

---

## 三峡集团 (eps.ctg.com.cn)

**入口**: `https://eps.ctg.com.cn/cms/channel/1ywgg1/index.htm`

**列表结构**:
- `#list1` > `li[name='li_name']` > `a[href]`
- href: `/cms/channel/1ywgg1/{articleId}.htm`
- 标题: `a span`，日期: `a em:last-child`

**详情URL**: `https://eps.ctg.com.cn/cms/channel/1ywgg1/240636631.htm`（直接可访问，无需登录）
- 内容选择器: `.article-content`
- 标题: `h1, h2`
- 正文完整可爬

**搜索**: 搜索框 `#inp-txt`（name=q），按钮 `#btnSearch`，跳转到 `/search.jspx?q=BIM`
- 搜索跳转到独立搜索页，不对列表页做过滤
- 搜索结果是独立页面，需要用 webbridge_interactive + navigate 方式

**分页**: page_number 模式，pageParam=pageNo，pageSize=10
