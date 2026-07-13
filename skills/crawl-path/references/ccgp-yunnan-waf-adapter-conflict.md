# 云南省政府采购网 (ccgp_云南省) — WAF + 适配器冲突

site_id: `ccgp_云南省`

## 问题

1. **WAF 云防护**：访问 `http://www.ccgp-yunnan.gov.cn/` 任何路径都返回 403「服务器拒绝执行该请求」，含云防护 ID（如 `1783190845603-268973fcdc9db669-19101`）
2. **适配器 vs 规则冲突**：即使适配器 `ccgp_provincial.py` 有完善的 `_fetch_yunnan_api`（Bootgrid POST in browser），只要 crawl_rules 有 `list_page`，同步流程就走规则跳过适配器
3. **纯 HTTP API 被验证码拦截**：直接 POST Bootgrid API 响应为空

## 适配器中的正确实现

`src/adapters/ccgp_provincial.py` 中的 `_fetch_yunnan_api` 方法：
- 使用 Playwright `page.request.post()`（浏览器会话内的 HTTP 请求）
- API：`/api/procurement/Procurement.gghtMoreList.svc`
- 参数：`{current, rowCount, query_type}`（query_type=1 招标公告）
- 解析 `rows[]` 中的 `bulletin_id` → 构造 `/showBulletinInfo.html?bulletin_id={id}` 详情 URL
- 日期从 `finishday` 字段解析

## 根因

同步流程（`BaseAdapter.run()`）的优先级逻辑：
```python
if rule and rule.list_page:
    # 走 RuleExecutor（规则驱动）— 跳过适配器
else:
    # 走 self.fetch_list() — 走适配器
```

## 修复

删除 `config/crawl_rules/ccgp_云南省.yaml` 中的 `list_page` 段。保留 entry_url/detail/limits 等字段。

## 限制

即使修复了适配器 vs 规则冲突，WAF 仍会阻断 Playwright 浏览器发起的 API 请求（IP 级封锁）。真正能跑的只有：
1. 用户真实浏览器（WebBridge）内的操作
2. 从云南省财政厅内网或白名单 IP 发起的请求
