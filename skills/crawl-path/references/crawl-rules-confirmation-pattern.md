# Crawl Rules 确认更新模式

当用户问「确认下是否已经更新任务规则yaml」时，这是一个**双重验证 + 立即行动**的模式。

## 确认流程

1. **API 层面确认** — `crawl_get_rule(site_id)` 检查：
   - `valid: true`（Pydantic 校验通过）
   - `data.yaml` 中的关键字段（entry_url、list_page.api.params.keyWords、pagination 等）符合预期
   - 这是后端加载的版本，反映服务实际使用的规则

2. **文件系统层面确认** — `read_file(path='config/crawl_rules/{site_id}.yaml')` 检查：
   - 实际磁盘文件与 API 返回一致
   - 确认写入持久化（不仅仅是缓存）
   - 这是备份/版本控制的版本，反映实际落盘的文件

3. **对比旧值总结** — 使用对比表格显示变更：
   | 项目 | 旧值 | 新值 |
   |------|------|------|
   | entry_url | `/consult/notice` | `/search` |
   | API 参数 | 无 keyWords | `keyWords: "BIM"` |
   | 等 |||

4. **立即行动** — 确认后询问或执行下一步：
   - 如果规则是新改的 → 建议试跑验证：`crawl_test(max_pages=1)`
   - 如果用户说「启动爬取任务」→ 立即 `crawl_trigger(site_id)`
   - 如果用户说「继续」→ 执行之前计划中的后续动作

## 已知案例

### 电建 search 入口 + BIM 搜索（2026-07-05）

用户要求：公共资源交易服务平台规则改为到 `/search` 页面搜索 BIM 关键字

**确认方法**：
```
crawl_get_rule("中国电力建设集团有限公司_公共资源交易服务平台")
  → valid: true
  → entry_url: https://bid.powerchina.cn/search
  → list_page.api.params.keyWords: "BIM"

read_file("config/crawl_rules/中国电力建设集团有限公司_公共资源交易服务平台.yaml")
  → 文件系统与 API 一致
```

**变更摘要**：
- `entry_url`: `/consult/notice` → `/search`
- `url_template`: path `/consult/notice` → `/search`
- API/分页/详情：保持不变（前端 `/search` 和后端 `allList` 调用同一个后端 API）

**验证**：`crawl_test(max_pages=1)` 返回 20 条 BIM 相关公告，URL 均以 `/search` 路径构建。

## 用户偏好

- 不要说「是的已创建」就完事 — 要给出带具体数据的对比总结
- 确认完成后，如果用户接下来有「启动爬取」或「继续」等指令，**立即执行**，不要停在状态确认上
- 如果 rules 有问题（valid=false、字段不对、文件不存在），直接提出修复方案，不等用户问
