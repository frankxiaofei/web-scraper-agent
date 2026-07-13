# BIM 每周增量爬取检查清单

## 站点列表

| 站点（site_id） | 类型 | BIM 获取方式 | 本周示例产量 |
|----------------|------|-------------|-------------|
| 中国交通建设集团有限公司_供应链管理信息系统 | dlzb 子站 Destoon | `skill_fetch_list_page` 爬5页，BIM 在告示标题/正文中，由 nightly bim_sync 打标 | ~1 条/周 |
| 中国能源建设集团有限公司_电子采购平台 | Next.js SSR (dnezb.com) | WebBridge 搜索 `kw=BIM&si=242`（能建频道），evaluate 提取详情 | ~1 条/周 |
| 中国铁道建筑集团有限公司_物资采购网（dlzb） | dlzb 子站 Destoon | `skill_fetch_list_page` 爬5页，BIM 靠 bim_sync 打标 | ~0 条/周（多为物资采购） |
| dlzb_power（电力招标网） | dlzb 父站 | 搜索页定时sync 已覆盖，BIM 通过 bom_sync 打标 | 已有 24 条 |
| bid.powerchina.cn（中国电建） | SPA + API | 走 `fetch_keyword_list_page("BIM")` HTTP API | ~77 条 BIM 标题 |

## 每周爬取流程

### 步骤 1：zgjtjs.dlzb.com（中交建）

该站 Destoon CMS，page_number 分页 `/v1/N/`，max_pages=5 覆盖全量：
```
skill_fetch_list_page(page_num=1..5, site_id="中国交通建设集团有限公司_供应链管理信息系统")
→ 每页 items[] 均为通用公告
→ BIM 内容在公告正文中，由 nightly bim_sync (03:00) 打标
→ 如果标题含 BIM，也可以手动 skill_save_notice + 主动存档
```

### 步骤 2：ceec.dnezb.com（中能建）— WebBridge 搜索

定时 sync 因 Next.js SSR 选择器不匹配返回 0 条。BIM 数据通过 WebBridge 搜索获取：

```
1. skill_webbridge_check  # 确认 daemon + extension 可用
2. skill_webbridge_navigate(url="https://www.dnezb.com/search?kw=BIM&si=242")
3. 提取详情列表 + 日期 → 筛选本周新增
4. 对每条新公告：
   a. skill_webbridge_navigate(url=detail_url)  # 同一 tab
   b. skill_webbridge_evaluate(code="document.body.textContent.substring(0, 3000)") → 正文
   c. skill_save_notice(site_id="中国能源建设集团有限公司_电子采购平台", ...)
5. 翻页检查：a[aria-label="Go to page 2"], page 3, page 4（最多 4 页 BIM 结果）
6. skill_webbridge_close(close_session=true)
```

### 步骤 3：汇总并发送

```
1. crawl_query_notices(bim_only=true, per_page=200) 获取全库 BIM
2. 用 Python 或手动筛选 publish_date >= 上周一 的条目
3. 按来源分组 → 格式化报告
4. crawl_notify_user 发送到飞书/CLI
```

## 已知限制

- **ceec.dnezb.com** 的定时 sync 返回 0 条。每轮 Sync 走 Playwright 规则 `container: .search-result-list, .notice-list, .el-table__body, table tbody` 和 Next.js 动态渲染的 class hash 不匹配。**不可通过 crawl_trigger 修复**，必须 WebBridge 搜索。
- **zgjtjs.dlzb.com** 的 5 页只覆盖到约 1 个月前。如果用户需要更久远的历史数据，需增大 `max_pages`。
- **oa.hdec.com** 为华东勘测设计研究院内部 OA 系统，不是公招平台，跳过。

## BIM 产量参考（正常运行周）

| 站点 | 典型每周 BIM 量 |
|------|---------------|
| 中交建（zgjtjs.dlzb） | 0-1 条 |
| 中能建（ceec.dnezb） | 0-2 条 |
| dlzb_power（电力招标网） | 由定时 sync 覆盖，bim_sync 打标 |
| bid.powerchina.cn（电建） | 由定时 sync 覆盖，bim_sync 打标 |
| **总计** | **约 2-4 条/周** |
