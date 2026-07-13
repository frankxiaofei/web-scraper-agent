# CCGP Provincial — bootgrid + captcha 站点集成

## 站点特征

部分省级政府采购网（云南、四川等）使用 **bootgrid** 表格插件 + **captcha 验证码** 加载数据：

- **API**：POST 到服务端某个 `.svc` 端点
- **参数**：`current`(页码), `rowCount`(每页条数), `query_type`(分类编码) 等
- **响应**：bootgrid 标准 JSON 格式 `{current, rowCount, rows, total}`
- **列表项**：每个 `<tr>` 含 `<a class="show">`，带 `data-bulletin_id`、`data-bulletinclass`、`data-tabletype`
- **详情 URL**：`/ggmxinfo.html?bulletinid={bulletin_id}`
- **分类切换**：通过 JS 函数 `userful(N)` 设置 `sign` 参数（1=招标公告, 2=结果公告, 23=采购意向等）
- **验证码**：直接 HTTP 调用 API 会被拦截（"验证码校验不通过，当前请求禁止访问"）

## 适配器集成步骤

### 1. 注册列表路径

在 `src/adapters/ccgp_provincial.py` 的 `SITE_LIST_PATHS` 中添加：

```python
"ccgp_云南省": ["http://www.ccgp-yunnan.gov.cn/"],
```

### 2. 编写 JS 提取函数

因为站点有 captcha，不能直接调 API。通过 `_fetch_with_js_scraper` 在浏览器中等待 AJAX 加载完成，然后提取已渲染的表格 DOM：

```python
_YUNNAN_JS = """() => {
    const out = [];
    const rows = document.querySelectorAll('#bulletinlistid tbody tr');
    for (let i = 0; i < rows.length; i++) {
        const a = rows[i].querySelector('a.show');
        if (!a) continue;
        const title = (a.innerText || '').trim();
        if (!title || title.length < 8) continue;
        const cells = rows[i].querySelectorAll('td');
        const dateText = cells.length >= 4 ? (cells[3].textContent || '').trim() : '';
        const bulletin_id = a.getAttribute('data-bulletin_id');
        const href = bulletin_id ? '/ggmxinfo.html?bulletinid=' + bulletin_id : '';
        out.push({ title, href, dateText });
    }
    return out;
}"""
```

### 3. 注册 JS scraper

在 `_SITE_SCRAPERS` 字典中注册，使用 `networkidle` 等待策略（确保 AJAX 加载完成）：

```python
_SITE_SCRAPERS: dict[str, tuple[str, str]] = {
    # ... 其他省份
    "ccgp_云南省": ("networkidle", _YUNNAN_JS),
}
```

### 4. 启用站点

在 `config/sites.yaml` 中将站点 `enabled: false` 改为 `true`。

### 5. 可选：保存 crawl_rules YAML

虽然适配器已能处理，保存规则便于 WebBridge 调试和规则面板查看：

```yaml
# config/crawl_rules/ccgp_云南省.yaml
version: 1
site_id: ccgp_云南省
name: 云南省政府采购网
enabled: true
entry_url: http://www.ccgp-yunnan.gov.cn/page/procurement/procurementList.html
list_page:
  strategy: dom_after_ajax
  container: "#bulletinlistid tbody"
  item: "tr"
  title: "a.show"
  link: "a.show"
  wait_for: "#bulletinlistid tbody tr"
detail:
  fetch_detail: true
  url_pattern: "/ggmxinfo\\.html\\?bulletinid="
  content_selector: ".purPage"
```

## WebBridge 勘探步骤

当需要为新 CCGP 省级站识别结构时：

1. `skill_webbridge_navigate(url)` — 打开站点首页
2. 通过 snapshot 或截图找到「采购信息」→「招标/预审/谈判/磋商/询价公告」导航路径
3. 点击进入列表页（可能需要找到正确分类链接）
4. `skill_webbridge_evaluate` 提取表格数据和分页信息：
   ```javascript
   // 查看表格行
   document.querySelectorAll('#bulletinlistid tbody tr').length
   // 查看总记录数
   document.querySelector('.bootgrid-footer')?.textContent
   // 提取公告 ID
   JSON.stringify(Array.from(document.querySelectorAll('a.show')).slice(0,3).map(a => ({
       title: a.textContent.trim(),
       bulletin_id: a.getAttribute('data-bulletin_id'),
       tabletype: a.getAttribute('data-tabletype')
   })))
   ```
5. 检查 API 请求格式（从 JS 源码中提取）：
   ```javascript
   // 查看 procurementList.js 中的 API URL
   fetch('/staticpage/procurement/xxx.js').then(r => r.text()).then(t => t.match(/url\s*:\s*["']([^"']+)/))
   ```
6. 检查是否有 captcha（用 fetch 直接调 API 看是否被拦截）

## 已知 CCGP 省级站列表

| site_id | 域名 | 类型 | 备注 |
|---------|------|------|------|
| ccgp_云南省 | www.ccgp-yunnan.gov.cn | bootgrid + captcha | 已接入 |
| ccgp_四川省 | www.ccgp-sichuan.gov.cn | REST API | 已接入（Sichuan API） |
| ccgp_广东省 | www.ccgp-guangdong.gov.cn | REST API (gpcms) | 已接入 |
| ccgp_上海市 | www.zfcg.sh.gov.cn | REST API | 已接入 |
| ccgp_江苏省 | www.ccgp-jiangsu.gov.cn | JS DOM | 已接入 |
| ccgp_浙江省 | www.ccgp-zhejiang.gov.cn | JS DOM | 已接入 |
| ccgp_湖北省 | www.ccgp-hubei.gov.cn | JS DOM | 已接入 |
| ccgp_河南省 | zfcg.henan.gov.cn | JS DOM | 已接入 |
| ccgp_北京市 | www.ccgp-beijing.gov.cn | 默认 fallback | 已注册路径 |
