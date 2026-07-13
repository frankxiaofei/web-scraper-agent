# 爬取步骤录制器（Chrome 扩展）

协助录制浏览器中的爬取操作，导出 JSON 后由项目 AI 生成 `config/crawl_rules/{site_id}.yaml`。

## 目录结构

```
chrome-extension/crawl-recorder/
├── manifest.json      # MV3 清单
├── background.js      # 录制状态、步骤存储
├── content.js         # 页面事件捕获（点击/导航/XHR）
├── selector.js        # CSS 选择器生成
├── popup.html/css/js  # 扩展弹窗 UI
├── icons/             # 16/48/128 图标
└── README.md
```

## 安装（加载 unpacked extension）

1. 打开 Chrome → `chrome://extensions/`
2. 开启右上角 **开发者模式**
3. 点击 **加载已解压的扩展程序**
4. 选择本目录：`web_scraper/chrome-extension/crawl-recorder/`
5. 工具栏出现「爬取步骤录制器」图标

## 使用流程

### 1. 录制

1. 在目标站点打开列表页或首页
2. 点击扩展图标，填写 **站点 ID**（如 `zycg_national`）
3. 点击 **开始录制**
4. 在页面中正常操作：
   - 点击菜单/链接进入列表
   - 点击翻页（自动标记 `pagination`）
   - AJAX 列表会自动捕获 `wait_network` 步骤
5. **Alt + 点击** 任意元素：记录其 CSS 选择器（用于 `list_container` 等 hints）
6. 点击 **停止录制**

### 2. 导出

- **复制 JSON**：复制到剪贴板
- **导出 JSON**：下载 `{site_id}_{timestamp}.json`

导出格式示例：

```json
{
  "site_id": "zycg_national",
  "entry_url": "https://example.com/list",
  "recorded_at": "2026-06-14T10:00:00.000Z",
  "steps": [
    {"action": "navigate", "url": "...", "timestamp": "..."},
    {"action": "click", "selector": "#nav a", "text": "采购公告", "timestamp": "..."},
    {"action": "wait_network", "url_pattern": "selectInfoMore.do", "timestamp": "..."},
    {"action": "click", "selector": "#page .next-page", "text": "下一页", "note": "pagination"}
  ],
  "hints": {
    "list_container": "#noticeShow",
    "sample_links": ["https://..."]
  }
}
```

### 3. 生成 YAML

**方式 A — Web UI**

1. 启动 Web UI：`python scripts/run_web_ui.py`
2. 打开 `/crawl-rules/{site_id}`
3. 拖拽 JSON 到「导入录制 JSON」区域，或点击选择文件
4. 右侧 YAML 预览自动填充，校验后保存

**方式 B — API**

```bash
curl -X POST http://127.0.0.1:8080/api/crawl-rules/generate-from-recording \
  -H 'Content-Type: application/json' \
  -d '{"site_id":"zycg_national","url":"https://...","recording":{...}}'
```

**方式 C — CLI**

```bash
python scripts/recording_to_yaml.py recording.json --site-id zycg_national --stub
python scripts/recording_to_yaml.py recording.json --site-id zycg_national -o config/crawl_rules/zycg_national.yaml
```

- 无 `OPENAI_API_KEY` 时自动使用 **stub 直转**（最小可用 YAML）
- 配置 LLM 后会将录制步骤作为上下文交给 `RuleGenerator`

## 选择器策略

`selector.js` 生成 CSS 选择器优先级：

1. `#id`（页面唯一）
2. `[data-*="..."]`
3. `tag.class` 唯一组合
4. 短路径 `parent > child`（最多 5 层）
5. `:nth-of-type` 作为兜底

避免过长 XPath，便于 crawl rules 维护。

## 权限说明

| 权限 | 用途 |
|------|------|
| `activeTab` | 当前标签页注入 content script |
| `storage` | 保存录制步骤 |
| `scripting` | 动态注入脚本 |
| `tabs` / `webNavigation` | 跟踪页面导航 |
| `<all_urls>` | 在任意站点录制 |

## 故障排除

- **步骤未记录**：确认已点击「开始录制」，且页面为 `http(s)://`
- **XHR 未捕获**：部分站点使用 sendBeacon/WebSocket，需手动 Alt+点击或后续在 YAML 中补充
- **重复 navigate**：同 URL 连续导航会自动去重
