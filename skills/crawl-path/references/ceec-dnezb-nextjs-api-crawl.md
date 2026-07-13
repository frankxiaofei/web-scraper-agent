# ceec.dnezb.com Next.js API 爬取记录

## 站点概况

- 站点: ceec.dnezb.com (中国能源建设电子采购平台)
- site_id: 中国能源建设集团有限公司_电子采购平台
- 架构: Next.js SSR (Server-Side Rendering) + MUI Pagination
- 公告分类 URL: https://ceec.dnezb.com/3001 (招标公告, noticeTypeCode=3001)
- 详情 URL 模板: `https://ceec.dnezb.com/detail/{articleId}`
- 总公告数: 3626 条 (招标公告分类)
- 每页: 15 条

## Next.js API 数据接口

### 获取 buildId

从页面的 `__NEXT_DATA__` 中提取：
```javascript
JSON.parse(document.getElementById('__NEXT_DATA__').textContent).buildId
// 结果: "e4a8a8e4bd9f4b1dbcfe2b82ca4d6263308964db"
```

### API URL 模板

```
/_next/data/{buildId}/ceec/{noticeCode}.json?noticeCode={noticeCode}&page={pageNum}
```

例如:
- 第1页: `/_next/data/e4a8a8e4bd9f4b1dbcfe2b82ca4d6263308964db/ceec/3001.json?noticeCode=3001&page=1`
- 第2页: `/_next/data/e4a8a8e4bd9f4b1dbcfe2b82ca4d6263308964db/ceec/3001.json?noticeCode=3001&page=2`

### API 响应结构

```json
{
  "pageProps": {
    "initialState": {
      "siteNoticeCodeArticles": {
        "data": {
          "result": 0,
          "totalCount": 3626,
          "articles": [
            {
              "articleId": 41188239,
              "title": "中能建定安县龙河镇大岭建筑用花岗岩矿项目...",
              "noticeTime": 1782662400000,
              "noticeTypeCode": 3001,
              "provinceCode": 0,
              "cityCode": 0,
              "siteId": 242,
              "purchaserCompanyId": null,
              "purchaserCompanyName": "",
              "projectId": "PCID-GC-DAXMGS-DAXMGS-2026-001",
              "pinMuType": 2,
              "cutOffTime": 1783040445000
            }
          ]
        }
      }
    }
  }
}
```

### 文章字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| articleId | int | 公告ID，用于构造详情URL |
| title | string | 公告标题 |
| noticeTime | long | 发布时间戳(ms) |
| noticeTypeCode | int | 公告类型代码(3001=招标公告) |
| purchaserCompanyName | string|null | 招标人名称 |
| projectId | string | 项目编号 |
| pinMuType | int | 品类类型(1=物资, 2=工程, 3=服务) |
| cutOffTime | long | 投标截止时间戳(ms) |
| provinceCode | int | 省份代码(0=全国) |
| siteId | int | 站点ID(242=能建) |

## 关键限制

### 外部 curl 触发 429 Too Many Requests

**已验证**：即使设置正确的 User-Agent 和 Referer，外部 HTTP 客户端（curl / urllib）直接请求 `/_next/data/{buildId}/...json` 时，第1页可获取，page≥2 返回 429。

**解决方案**：
1. **WebBridge 浏览器中 fetch** — 在 WebBridge 会话中用 `fetch()` 调用同源 API，利用浏览器已有的 Cookie 和 User-Agent：
   ```javascript
   async function fetchAllPages() {
     const bid = JSON.parse(document.getElementById('__NEXT_DATA__').textContent).buildId;
     const allArticles = [];
     const totalPages = 242;
     for (let page = 1; page <= totalPages; page++) {
       const url = `/_next/data/${bid}/ceec/3001.json?noticeCode=3001&page=${page}`;
       const resp = await fetch(url);
       const data = await resp.json();
       const articles = data?.pageProps?.initialState?.siteNoticeCodeArticles?.data?.articles || [];
       allArticles.push(...articles);
       if (page % 10 === 0) await new Promise(r => setTimeout(r, 1000));
     }
     return allArticles;
   }
   ```
2. **等待冷却期** — 配置 `rate_limit_seconds: 180` 后分批请求

### WebBridge 搜索关键词在子站域无结果

**问题**：`https://ceec.dnezb.com/search?q=BIM` 页面加载后，main 区域只显示筛选器（地区按钮、公告类型标签），无搜索结果列表。
- `skill_webbridge_extract_list` → 返回 0 条
- `skill_webbridge_evaluate` 检查 DOM → main 中只有筛选 UI，无搜索结果 DOM 节点
- 搜索结果可能通过 JS 动态加载但失败了（未触发 API 调用）

**原因推测**：
- ceec.dnezb.com 是电力招标网(dnezb.com)的一个子站域
- 该域名下的搜索 API 可能未对接回能建(/ceec/)频道的数据
- 或搜索需要用户登录才能触发

**替代方案**：使用父站 `www.dnezb.com/search?q=BIM` 搜索获取全站结果（但不限于能建频道）。

## 翻页策略

页面使用 MUI Pagination，分页链接为 `<a href="/3001?page=N">`。

## 最新数据观察 (2026-07-02)

| 指标 | 值 |
|------|-----|
| 最新 articleId | 41241712（河套主变扩建项目，2026-07-02） |
| 分类 totalCount | ~3626（未变） |
| articleId 增长 | 从~4118万→~4124万，每日常量新增 |
| DB 中 BIM 公告 | 26 条（通过搜索关键词聚合，非 API 全量扫描） |

## 站点头部页面（首页无关键词搜索）

直接导航到 `https://ceec.dnezb.com/search?si=242`（通过 WebBridge 点击导航栏的"能建招标"链接）时，页面会加载**该站点的完整公告列表**。

此时 `__NEXT_DATA__` 中的数据结构是：

```javascript
// 数据路径
__NEXT_DATA__.props.pageProps.initialState.siteZBArticlesList.data.siteZBArticles[siteId]
// 例如 siteId=242:
// state.siteZBArticlesList.data.siteZBArticles['242']
// {
//   articles: [...],  // 当前页的24条公告
//   totalCount: 7562  // 站点总公告数
// }
```

注意：**与 `/search` 首页的直接回复不同**，通过 URL `search?si=242&kw=BIM` 搜索后，数据在 `searchArticlesList.data.articles` 中；而直接访问 `search?si=242`（无 kw 参数）时，数据在 `siteZBArticlesList.data.siteZBArticles[siteId]` 中。这两个是不同的列表。

除了全局 noticeCode 分类 API，ceec.dnezb.com 还有**站点级别**的搜索 API，按 `si={siteId}` 参数过滤。

### API URL 模板（站点搜索）

```
/_next/data/{buildId}/search.json?si={siteId}&page={pageNum}
```

例如:
- 能建 (siteId=242) 第1页: `/_next/data/{buildId}/search.json?si=242&page=1`
- 能建 BIM 搜索: `/_next/data/{buildId}/search.json?si=242&kw=BIM&page=1`
  （输入搜索框后地址栏变为 `https://www.dnezb.com/search?kw=BIM&si=242`）

### API 响应结构（站点搜索）

```json
{
  "pageProps": {
    "searchKey": "BIM",
    "siteId": 242,
    "initialState": {
      "siteZBArticlesList": {
        "data": {
          "siteZBArticles": {
            "242": {
              "totalCount": 7562,
              "articles": [...]
            }
          }
        }
      },
      "searchArticlesList": {
        "data": {
          "articles": [...],
          "totalCount": 77,
          "accurateCount": 77
        }
      }
    }
  }
}
```

### 关键数据路径

| 场景 | 数据路径 | 总条数 | 每页 |
|------|----------|--------|------|
| 站点全量 | `siteZBArticlesList.data.siteZBArticles[siteId].articles` | 7562 | 24 |
| 关键词搜索 | `searchArticlesList.data.articles` | 取决于关键词 | 24 |
| 全局分类 | `siteNoticeCodeArticles.data.articles` | 3626 (noticeCode=3001) | 15 |

### 站点 ID 对应关系

| siteId | 站点名称 |
|--------|----------|
| 242 | 中能建 (ceec.dnezb.com) |
| 243 | 待确认（其他子站） |

## BIM 公告搜索方法

### 方式一：站点关键词搜索（推荐，覆盖最全）

在能建 (si=242) 搜索 BIM 关键词，共找到 **77 条 BIM 相关公告**（accurateCount=77）。

**操作步骤**（WebBridge）：
1. `skill_webbridge_navigate(url='https://ceec.dnezb.com/search?si=242')`
2. `skill_webbridge_fill(selector='@e5', value='BIM')` — 输入搜索框
3. `skill_webbridge_click(selector='@e6')` — 点击搜索
4. 等待 2-3 秒
5. `skill_webbridge_evaluate(code="JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps.initialState.searchArticlesList.data.articles")` — 提取搜索结果

获取到的数据包含标题（带 `<span style=color:red>` 高亮标记）、articleId、noticeTime 等。

**BIM 公告示例**（能建 77 条中的最新）：
- SketchUpPro软件（articleId: 41190209, 2026-06-29）
- 西安草堂房建项目BIM服务询比采购（articleId: 40947680, 2026-06-13）
- 东电一公司广东太平岭核电厂BIM平台及PDMS软件服（articleId: 40932190, 2026-06-12）

### 方式二：全局分类扫描（旧方法，低效）

关键词范围：`['BIM','bim','建筑信息模型','数字孪生','数字化移交','三维设计','三维建模','三维效果图','三维模型','Revit','Tekla','Navisworks','数字电厂','智慧工地','智能建造']`

前50页（750条）中找到2条 BIM 相关公告：
1. 综合采购（科技信息部）2026-2028年变电站三维效果图制作技术服务（框架）招标公告
   - articleId: 29841771, 时间: 2026-04-14
   - 招标人: 中国能源建设集团甘肃省电力设计院有限公司
2. 中煤岱山鱼山电厂、中煤玉环三期扩建工程EPC总承包项目数字化电厂移交和服务招标公告
   - articleId: 29810811, 时间: 2026-04-12
   - 招标人: 中国能源建设集团浙江省电力设计院有限公司

**方式一（站点关键词搜索）比方式二（全局扫描）更加高效**——77 条 vs 2 条，覆盖率高 38 倍。

## 详情页访问限制

详情页（`https://ceec.dnezb.com/detail/{articleId}`）需要登录才能查看全文。
`__NEXT_DATA__` 中 `isShowDetail:0, isCanLookDetail:0` 表示当前用户无权限。
只能提取元数据（标题、日期、招标人、项目编号）保存到系统，正文为空。

## crawl_rules 现状

当前的 crawl_rules 使用通用 DOM 选择器去 `/search` 页面（entry_url: https://ceec.dnezb.com/search），但该页面是客户端渲染搜索页，Playwright 的 DOM 选择器解析到0条。需要重写规则为 API 策略（strategy: api）才能正常工作。
