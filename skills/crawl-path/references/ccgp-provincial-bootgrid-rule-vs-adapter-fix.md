# CCGP 省级站规则-适配器优先级修复 — 云南站模式

## 场景

云南省政府采购网（ccgp_云南省），Bootgrid SPA 站点，翻页有滑块验证码。

## 诊断过程

1. crawl_get_task_status / crawl_get_run_logs 确认每次 sync 只拿到 0-1 条
2. 日志显示「规则驱动抓取列表」（非适配器路径）
3. WebBridge navigate 确认翻页弹出验证码
4. 已有 _fetch_yunnan_api 适配器实现（Bootgrid REST API），但从未被调用
5. 检查 BaseAdapter.run() 发现：有 rule.list_page 时优先走 RuleExecutor

## 修复步骤

### 1. 删除规则中的 list_page

config/crawl_rules/ccgp_云南省.yaml 删除 list_page 段（翻页有验证码），保留 entry_url, detail, limits。

### 2. 修改适配器分支

src/adapters/ccgp_provincial.py:
```python
if self.site_id == "ccgp_云南省":
    return await self._fetch_yunnan_api(max_items)  # 不走 JS scraper
```

### 3. 修复纯 HTTP API 翻页限制（可选）

src/core/rule_executor.py _collect_api_pages_impl 中补充 page_number 翻页类型支持（见 crawl-path 的「API 站关键陷阱」）。

## 验证

- 触发 sync：crawl_trigger(site_id="ccgp_云南省")
- 检查日志含「API 分页」而非「规则驱动」
- crawl_poll_until_done 确认 notices_count > 0
- crawl_query_notices(site_id="ccgp_云南省") 确认数据入库

## 适用类型

同类 Bootgrid SPA 省级 ccgp 站（ccgp_*）如果翻页有验证码且适配器已有 API 实现。
