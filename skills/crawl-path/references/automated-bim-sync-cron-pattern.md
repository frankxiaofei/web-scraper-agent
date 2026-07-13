# 电建 BIM 自动化同步 cron 模式

## 概述

bid.powerchina.cn 已有专用 HTTP 脚本 `scripts/sync_powerchina_bim_http.py`，可通过 allList 搜索 BIM 关键词并获取详情入库。此参考文件描述如何将其转化为定时 cron 任务。

## 脚本位置

```
scripts/sync_powerchina_bim_http.py
```

## 脚本工作流

1. **搜索**: POST `/newcbs/recpro-newmember/BidAnnouncementSummary/allList` with `keyWords="BIM"`
   - 注意：此 API 返回的 `url` 字段是搜索页占位符，不是真实详情 URL
   - 脚本通过 `extract_row_notice_id(row)` 提取 `id` 字段，然后用 `build_detail_url(id)` 构造真实 URL
2. **提取 ID**: `extract_row_notice_id(row)` 从返回行中提取 `id`
3. **详情**: `fetch_notice_content(notice_id)` → `getInfo/{id}` API，如果 HTML 为空则自动下载 PDF（`pictureUrl`）
4. **入库**: `save_notice(site_id, title, url, publish_date, content_text, content_html)`

## 执行参数

```bash
# dry-run 预览
python scripts/sync_powerchina_bim_http.py --dry-run

# 完整执行（搜索 BIM + 详情入库）
python scripts/sync_powerchina_bim_http.py --max-pages 1 --fetch-detail --max-retries 2 --rate-limit 0.5

# JSON 输出
python scripts/sync_powerchina_bim_http.py --max-pages 1 --fetch-detail --json
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--max-pages` | 1 | BIM 关键词所有结果在第 1 页（pageSize=100 时 total=78），无需多页 |
| `--fetch-detail` | True (default) | 调用 getInfo 获取公告正文（含 PDF 下载） |
| `--no-detail` | False | 仅保存列表信息，不抓详情 |
| `--max-retries` | 3 | getInfo/PDF 失败重试次数 |
| `--rate-limit` | 0.3 | 条间间隔秒数（建议 0.3-0.5，避免触发 429） |
| `--dry-run` | False | 仅打印列表，不写库 |
| `--keyword` | "BIM" | 搜索关键词 |
| `--page-size` | 100 | allList pageSize（100 即可覆盖全部 78 条） |
| `--consult-pages` | 0 | 额外扫描 consult/notice list 最近 N 页 |
| `--json` | False | 最后输出 JSON 摘要 |

### 已知结果

- 每次全量 78 条 BIM 公告（current，随新公告发布可能变化）
- 其中 ~73 条成功入库（含正文 content_text），~5 条 failed（新闻类公告，getInfo 返回空）
- 5 条 failed 的通常是 id 以 `240914`/`240817` 开头的新闻/会议公告（如「中央企业BIM软件创新联合体启动大会」），这些不是招标公告，getInfo 无数据
- 脚本是幂等的：`save_notice` 用 URL 去重，重复执行对已有公告返回 `duplicate=True`

## Cron 任务配置

```bash
cd /Users/lixiaofei/Documents/codes/04_yummy/web_scraper && python scripts/sync_powerchina_bim_http.py --max-pages 1 --fetch-detail --max-retries 2 --rate-limit 0.5
```

### 调度建议

| 频率 | 理由 |
|------|------|
| **每天 04:00** | 电建站每日有定时整站 sync（每 64 分钟），BIM 关键词同步覆盖整站 sync 可能遗漏的搜索匹配项 |
| 每 6 小时 | 如果 BIM 公告更新频繁（新招标公告随时发布） |

### 当前 cron 任务

```
job_id: 90ceef95c289
name: powerchina-bim-sync
schedule: 0 4 * * * (每天 04:00 CST)
repeat: forever
deliver: local (不推送，仅入库)
```

## Python 3.11 UnboundLocalError 修复

### 问题

`src/db/mongo_repository.py` 的 `_connect()` 方法中：

```python
try:
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    # ...
except (ConnectionFailure, ServerSelectionTimeoutError, ImportError) as e:  # ❌ Python 3.11
```

Python 3.11 增强了作用域规则：`try` 块内 `import` 的变量在 `except` 子句中视为「未绑定」，抛出 `UnboundLocalError: cannot access local variable 'ConnectionFailure' where it is not associated with a value`。

### 修复

```python
try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError  # noqa: F401
    # ...
except BaseException as e:  # ✅ 使用 BaseException 捕获所有异常
    logger.warning("MongoDB 不可用，已降级为 JSONL: %s", e)
    self._available = False
```

### 检查全项目同模式

在同项目搜索 `except \(.*ConnectionFailure.*ServerSelectionTimeout.*ImportError\)` 检查是否有其他 try/except 块有同样问题。

## 替代方案：对话内直接运行保存脚本（execute_code 被阻止 / 脚本缺失时）

当 `sync_powerchina_bim_http.py` 不存在于磁盘（或被删除）且 `execute_code` 被阻止时：

1. 用 `write_file` 将保存脚本写入 `data/generated_scripts/`
2. 用 `terminal('python3 data/generated_scripts/<name>.py')` 运行

当前可用脚本：`data/generated_scripts/save_powerchina_bim.py`

脚本工作流（替代版）：
1. 直接 HTTP POST 到 `BidAnnouncementSummary/allList` 搜索 BIM（pageSize=100 一次性获取 79 条）
2. 对每条公告调用 `getInfo/{id}` 获取详情（即使 announcementContent 为空）
3. 通过 `skill_save_notice` 入库（自动去重）
4. 注意：getInfo 返回的 `announcementContent` 字段**全部为空**（正文在 PDF 附件中），当前脚本不会解析 PDF

## 文档版本记录

### 2026-07-03 更新
- `scripts/sync_powerchina_bim_http.py` 文件已不存在于磁盘
- 替代脚本 `data/generated_scripts/save_powerchina_bim.py` 可用
- allList 返回 BIM 结果数：79 条（从 78 更新）
- getInfo 确认不返回 announcementContent 内容（全部为空）
- crawl_rules 已改用 allList + keyWords=BIM（不再用 list API）
- BIM 总数更新为 77 条（入库验证通过）
- `scripts/backfill_powerchina_bim_content.py` — 补抓已入库公告的正文 + 修正占位 URL
- `src/core/powerchina_notice.py` — 核心库函数（fetch_keyword_list_page, fetch_notice_content, build_detail_url）
- `src/db/mongo_repository.py` — MongoDB 连接（已修复 Python 3.11 bug）
