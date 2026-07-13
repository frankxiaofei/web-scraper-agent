# 电建 BIM 公告爬取参考

## 站点状态

- **site_id**: `中国电力建设集团有限公司_公共资源交易服务平台`
- MongoDB 数据库 120+ 条公告，BIM 标记 75 条（is_bim_related=true）
- BIM 标记来源：daily_bim_sync (LLM 正文分析)，非标题关键词匹配
- 标题含 "BIM" 公告: 27 条（2025-08 ~ 2026-06，见下方入库记录）

## Stale Sync 修复实录

### 问题
站点状态显示 `syncing (stale=true)` — 一个 50 小时的 sync 卡在 `fetch_detail (108/120)`。
后续定时任务（每 64 分钟触发一次）叠加在 stale sync 上，都无法继续，形成死锁。

### 修复步骤
1. `crawl_reset(site_id="中国电力建设集团有限公司_公共资源交易服务平台")` — 清除 stale 状态
2. `crawl_trigger(site_id="...")` — 手动触发新 sync
3. `crawl_poll_until_done(timeout=600)` — 等待完成（超时 600s）
4. Check `crawl_get_task_status()` — 发现最后一次 poll 后又有新的 scheduled trigger 启动
5. 再次 `crawl_reset()` — 清除第二次残留的 stale

### 关键教训
- **重置后不要立即 poll** — 定时调度可能已排队，手动 + 定时两个 trigger 同时跑，容易再次 stale
- **两次 reset 模式**：第一次清除旧的 stale，跑完一轮后 check 状态，如果有新的 concurrent run 残留，再 reset 一次
- **crawl_poll_until_done 的 timeout 最好设为 300-600s** — 本次 sync 约 600s 完成

## BIM 内容查询模式

- `crawl_query_notices(keyword="BIM", ...)` — 搜索全文内容含 BIM 的公告。返回 75 条（正文提及 BIM）
- `crawl_query_notices(bim_only=true, ...)` — 搜索 is_bim_related=true 的公告。也是 75 条
- 标题含 "BIM" 的公告：0 条（搜索 API 能找到 77 条标题含 BIM 的公告，但从未入库 — 见下方「搜索 API 陷阱」）

## 2026-07-03 更新：crawl_rules 改用 allList + keyWords=BIM

### 变更内容

crawl_rules (`config/crawl_rules/中国电力建设集团有限公司_公共资源交易服务平台.yaml`) 已更新：
- `list_page.api.url` 从 `BidAnnouncementSummary/list` 改为 `BidAnnouncementSummary/allList`
- 增加 `keyWords: "BIM"` 参数
- `pagination.max_pages` 从 6 降为 5
- `limits.max_items` 从 120 降为 100

### 效果

`skill_fetch_list_page` 现在直接返回 BIM 搜索命中的公告（79 条，分 4 页），不再遍历全部 120 条无关键词的公告。

### 剩余问题

`getInfo/{id}` API 返回的 `announcementContent` 字段**全部为空字符串**。正文内容通过 `pictureUrl`/`pdfId` 字段以 PDF 附件形式提供。当前 batch 入库脚本 `data/generated_scripts/save_powerchina_bim.py` 会尝试获取 `announcementContent` 但通常得到空值。后续如需正文可补充 PDF 解析逻辑。

### 入库验证

2026-07-03 执行结果：
- 脚本：`data/generated_scripts/save_powerchina_bim.py`
- allList 搜索到：79 条
- getInfo 获取详情到：79 条（均无正文）
- skill_save_notice 去重后：1 条新增（78 条已存在）
- BIM 总数：76 → 77 条

## 两套搜索 API 的分工

电建站点有 **两套 API** 可以进行关键词搜索，各自用途不同：

### API 对照表

| API | 参数 | 返回的 `url` 字段 | `id` 是否可用 | 用途 |
|-----|------|-------------------|---------------|------|
| `list` | `{pageNum, pageSize, keyWords, ...}` | **真实详情 URL** ✅ | ✅ | crawl_rules 配置使用，前端实际使用的搜索接口 |
| `allList` | `{keyWords, pageNum, pageSize}` | 搜索页占位符 ❌ | ✅ | `sync_powerchina_bim_http.py` 脚本使用，返回更多结果（78 条 vs 27 条） |

### 两类搜索的结果差异

| 条目 | `list` (`keyWords=BIM`, pageSize=20) | `allList` (`keyWords=BIM`, pageSize=100) |
|------|---------------------------------------|------------------------------------------|
| 返回条数 | 27 条 | 78 条 |
| 日期范围 | 2025-08 ~ 2026-06 | 2025-08 ~ 2026-06 |
| url 可信度 | ✅ 真实 detail URL | ❌ 占位符（需 `build_detail_url` 转换） |
| id 可信度 | ✅ | ✅ |

### 关键结论

1. 当前 `sync_powerchina_bim_http.py` 使用 `allList` API，内部通过三步保证入库 URL 可用：
   - `fetch_keyword_list_page` → `extract_row_notice_id` → `build_detail_url`
2. `crawl_rules` 配置使用 `list` API（url_template 直接返回真实 URL），路径不同但互补
3. 两者都是 keyword 搜索，区别是 `list` 的 `url` 字段可直接用，`allList` 的需转换

## 入库状态

### 脚本入库（sync_powerchina_bim_http.py，每日 04:00）

脚本 `scripts/sync_powerchina_bim_http.py` 实现了全流程：
1. 用 `allList` + `keyWords="BIM"` + `pageSize=100` 获取 78 条公告
2. 提取 `id`，调用 `getInfo/{id}` 获取正文 HTML
3. 通过 `build_detail_url(id)` 构造真实 URL 入库
4. 执行结果：~73 条成功入库（含正文），~5 条 failed（新闻类公告无 getInfo 数据）
5. 定时任务 `powerchina-bim-sync` 每日 04:00 执行（job_id: 90ceef95c289）

### 爬取规则入库（crawl_rules 配置，每 64 分钟 1 次）

通过站点的全量列表 API 获取 120 条公告（6 页 × 20 条），其中 LLM BIM 分类标记 3 条（`bim_count`）。

**两套路径互补**：脚本负责 BIM 关键词搜索补全，crawl_rules 负责整站全量覆盖。

## 2026-07-05 更新：entry_url 改为 /search 搜索页

### 变更内容

用户要求 crawl_rules 的入口改为 `https://bid.powerchina.cn/search`（搜索页面），并搜索 BIM 关键字。通过 WebBridge 验证：

1. **navigate 到 /search** — 加载成功，页面为 Element UI 搜索表单
2. **fill "BIM"** → **click 查询按钮** — API 调用 `allList`（与 `/consult/notice` 相同）
3. **performance.getEntriesByType('resource')** 证实后端调用的就是 `BidAnnouncementSummary/allList` POST
4. 所以 entry_url 换为 `/search` 后，list_page API 配置完全不变

### 搜索结果页 DOM 结构

通过 WebBridge evaluate 探查到的搜索结果 DOM 结构：

```
.card-item
  .card-content
    .title      ← 公告标题（Vue @click 导航）
    .time       ← 发布日期
```

- **不是 `<a>` 标签** — 所有结果项是 Vue 组件渲染的 `div.card-item > div.card-content > div.title`
- **`skill_webbridge_click` 无法导航** — 点击 `.card-item` 或 `.title` div 不会触发 Vue Router 跳转（`isTrusted` 限制）
- **`skill_webbridge_extract_list` 返回 0 条** — 因为 DOM 中没有任何 `<a>` 标签
- 但 WebBridge snapshot 可以看到所有文本标题和日期

### 前端搜索 vs API 搜索的一致性

| 入口 | 后端 API | keyWords 参数 | 结果 |
|------|---------|---------------|------|
| 前端 `/search` 输入 BIM → 点击查询 | `allList` POST | `keyWords: "BIM"` | 20 条/页 |
| crawl_rules `allList` API | `allList` POST | `keyWords: "BIM"` | 20 条/页 |

两者同源同参数，crawl_rules 的 API 配置等效于用户在搜索页输入 BIM 点查询。

### 修改记录

crawl_rules YAML 变更：
- `entry_url`: `/consult/notice` → `/search`
- `url_template`: path 参数 `/consult/notice` → `/search`
- API/分页/详情参数：保持不变

## 调试技巧：WebBridge 拦截 XHR/fetch 发现隐藏 API 参数

当 SPA 页面的搜索功能比 API 文档描述的更强大时（例如 `keyword` 参数无效但前端能搜出结果），可通过 WebBridge 拦截浏览器网络请求，找到真正的 API 参数名。

### 拦截方法

```javascript
// 在 skill_webbridge_evaluate 中执行：

// 1. 拦截 XMLHttpRequest 的 send 方法
const origSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.send = function(body) {
  if (body && typeof body === 'string' && body.includes('pageNum')) {
    console.log('[XHR BODY]', body);
    window.__lastXHRBody = body;  // 保存到 window 供后续读取
  }
  return origSend.call(this, body);
};

// 2. 或者拦截 fetch
const origFetch = window.fetch.bind(window);
window.fetch = function(url, opts) {
  if (typeof url === 'string' && url.includes('BidAnnouncement')) {
    window.__lastFetchArgs = {url, body: opts?.body || ''};
  }
  return origFetch(url, opts);
};

// 3. 点击搜索按钮后，读取捕获的参数
// window.__lastXHRBody 或 window.__lastFetchArgs
```

### 本次会话的应用实例

前端搜索 BIM 时，原以为 API 参数是 `keyword`（从后端文档推测），但实际捕获的请求体显示参数名为 `keyWords`（驼峰）。这一发现直接解决了 27 条 BIM 公告的获取问题。

### 适用场景
- Vue/React SPA 站点的搜索/筛选功能
- 前端有搜索能力但后端 API 文档不完整
- 需要发现 API 的参数名大小写、拼写、可选字段

## SPA Vue Router 导航无法通过 WebBridge 触发

2026-06-29 发现并确认：**电建的搜索页面是 Vue + Element UI SPA，`skill_webbridge_click` 对 `<card-item>` 的点击不会触发路由跳转。**

### 尝试路径（全部失败）

| 方法 | 结果 |
|------|------|
| `skill_webbridge_click(@e ref)` 点击 card-item | 点击成功但页面不跳转 |
| `evaluate` 执行 `.click()` / `dispatchEvent(MouseEvent)` | 同上 — Vue Router 忽略非 `isTrusted` 事件 |
| `evaluate` 点击内部 `.title` div | 同上 |
| XHR/fetch 拦截（Monkeypatch `open`/`send`/`fetch`） | 页面使用了 axios 等网络库，拦截不生效 |

### 正确方案

1. **直接走 HTTP API** — 不需要 WebBridge
2. **通过 Vue Router push** — 用 `evaluate` 执行 `router.push({name: 'Notice', query: {id: noticeId}})`，前提是需要先知道公告 ID
3. **纯文本提取** — 用 `evaluate` 执行 `document.querySelector('main').innerText` 提取所有可见文本（无详情 URL）


## route 结构

| Component Name | Path | 说明 |
|----------------|------|------|
| Index | `/`、`/index` | 首页 |
| Search | `/search` | 搜索页（本文操作的主页面） |
| ConsultNotice | `/consult/notice` | 招采公告列表 |
| Notice | `/notice/detail` | 公告详情（query 参数 `id`） |
| ConsultArticle | `/consult/article` | 资讯文章列表 |
| Article | `/article/detail` | 文章详情 |
| Supplier / SupplierDetail | `/supplier` | 供应商相关 |

注意：Notice/Article 路由不带 path 参数，公告 ID 通过 `query.id` 传入。

## 搜索 API 参数格式

通过 `src.core.powerchina_notice` 确认：

```python
# 关键词搜索
ALL_LIST_URL = "https://bid.powerchina.cn/newcbs/recpro-newmember/BidAnnouncementSummary/allList"
payload = {
    "keyWords": "BIM",        # 搜索关键词
    "pageNum": 1,             # 页码（1-indexed）
    "pageSize": 20,           # 每页条数
    "publishStartTime": "",   # 起始日期（可选）
    "publishEndTime": "",     # 结束日期（可选）
}

# 全量分页
payload_all = {
    "curpage": 1,
    "pageSize": 20,
    "companyType": "3",
}
```

## 用户偏好

- **直接行动** — 当被告知路径阻塞时，不接受长篇解释，立即给替代方案
- **继续完成** — "继续完成爬取" 意味着持续执行，不是停下来等指示
