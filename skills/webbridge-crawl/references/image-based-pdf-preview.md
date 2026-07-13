# 图片式 PDF 预览爬取（CSS Sprite Tiles 与 PDF.js 渲染）

部分政府/标准网站（如 `openstd.samr.gov.cn` 国家标准公开系统）的 PDF 预览不是下载原生 PDF 文件，而是将 PDF 每页切分为 **CSS sprite 图块**（旧版）或用 **PDF.js**（新版）渲染为 canvas 图像。此类站点无法通过 `web_extract` 或下载工具直接获取 PDF 文本内容。

## 站点特征

### CSS Sprite Tiles 版（旧）
1. 预览按钮调用 `window.open("/path/showGb?type=online&hcno=ID")`
2. 直接导航到 `showGb` URL 会被 **referrer 检查** 重定向回详情页
3. 预览页使用自定义 PDF 查看器，无 `<canvas>`、`<embed>`、`<iframe>` 或 `<img>` 标签
4. 页面结构：`<div id="viewer" class="pdfViewer">` 包含多个 `<div class="page">`
5. 每个页面由多个 `<span class="pdfImg-N-M">` 子元素拼接

### PDF.js 版（新版 — openstd 当前使用）
1. 标准 PDF.js 查看器：`#outerContainer > #mainContainer > .toolbar + #viewerContainer`
2. 工具栏含：上一页/下一页按钮 (`#previous`/`#next`)、页码输入框 (`#pageNumber`)、总页数 (`#numPages`)、缩放选择器
3. PDF 内容通过 `<canvas>` 渲染（accessibility tree 不可见，但截图可捕获）
4. 总页数显示在 PDF 工具栏

## 核心问题：Referrer 检查

`showGb` URL 被 referrer 保护——直接 navigate 会 302 到 `newGbInfo?refer=outter`。有两种绕过方式：

### 方法 A：iframe 注入（已有方式，兼容旧版）

在详情页用 iframe 加载预览 URL，利用详情页自身的 referrer 通过检查：

```javascript
// 在详情页（newGbInfo?hcno=ID）的 WebBridge evaluate 中执行
var iframe = document.createElement('iframe');
iframe.style.position = 'fixed';
iframe.style.top = '0';
iframe.style.left = '0';
iframe.style.width = '1200px';
iframe.style.height = '900px';
iframe.style.zIndex = '9999';
iframe.src = '/bzgk/std/showGb?type=online&hcno=HCNO&request_locale=zh-CN';
document.body.appendChild(iframe);
```

**缺点**：iframe 可能被同源策略限制访问 contentDocument；CSS sprite 版需要额外的元素检测。

### 方法 B：hijack window.open ✅（推荐，更简单）

在点击"在线预览"按钮**之前**，劫持 `window.open` 使其在当前标签页导航，而不是打开新窗口：

```javascript
// 在详情页 WebBridge evaluate 中执行
window.open = function(url, name, features) {
    window.location.href = url;  // 在当前页导航
    return window;
};
// 然后点击按钮
document.querySelector('button.ck_btn').click();
```

这会让 PDF 预览在当前标签页打开，WebBridge 可以直接截图。

**优点**：
- 不需要 iframe
- WebBridge 截图工具直接工作（截图当前标签页）
- PDF.js 版支持翻页（`#next` 按钮点击）
- 支持获取 `#numPages` 总页数信息

**缺点**：
- 仅对 jQuery click 事件绑定有效（openstd 使用 `$('.ck_btn').click(function(){ showGb… })`）
- 不适用于 `window.open` 后子窗口 `postMessage` 通信的场景

## 推荐工作流（openstd.samr.gov.cn PDF.js 版）

```
1. skill_webbridge_navigate(url=详情页URL)   ← 导航到 newGbInfo?hcno=XXXX
2. skill_webbridge_evaluate 执行 hijack:
   window.open = function(url,n,f){ window.location.href=url; return window; };
   document.querySelector('button.ck_btn').click();
3. skill_webbridge_wait(seconds=5)           ← 等 PDF.js 加载渲染
4. skill_webbridge_evaluate 查询:
   document.getElementById('pageNumber').value  → 当前页
   document.getElementById('numPages').textContent → 总页数
5. 循环截图:
   5a. skill_webbridge_screenshot → 保存当前页截图
   5b. if page < total_pages:
       skill_webbridge_evaluate: document.getElementById('next').click()
       skill_webbridge_wait(seconds=2)
6. skill_webbridge_close(close_session=true)  ← 清理
```

## openstd.samr.gov.cn 站点特定信息

| 属性 | 值 |
|------|------|
| 站点 ID | `openstd_national` |
| 列表页 | `std_list_type?p.p1={page}&p.p90=circulation_date&p.p91=desc` |
| 每页条数 | 10 |
| 总条数 | 6120 |
| 总页数 | 612 |
| 列表 CSS 容器 | `table.result_list` |
| 列表 CSS 行 | `tr:not(:first-child)` |
| 详情 URL | `newGbInfo?hcno={hcno}` |
| 预览 URL | `showGb?type=online&hcno={hcno}&request_locale=zh` |
| 预览按钮 class | `.ck_btn`（class 可能变化） |
| 预览按钮文本 | "在线预览" |
| PDF 查看器 | PDF.js |
| 翻页按钮 ID | `#previous` / `#next` |
| 页码输入 | `#pageNumber` |
| 总页数元素 | `#numPages` |
| 爬取脚本 | `scripts/crawl_openstd_standards.py` |

## 已知限制

- 截图方式无法获取 PDF 的搜索/复制功能
- 39 页 PDF 需要多张截图
- 截图质量受浏览器窗口大小和缩放影响（建议 1280px 以上窗口）
- 仅 WebBridge 中可行（依赖真实浏览器渲染）

## 其他使用此模式的站点

- openstd.samr.gov.cn — 国家标准全文公开系统
- 其他政府标准公开平台（检查预览按钮是否 `window.open` + referrer 保护）
