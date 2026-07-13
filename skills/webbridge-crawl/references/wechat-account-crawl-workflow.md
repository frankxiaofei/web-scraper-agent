# 微信公众号爬取工作流与问题排查

## 总览

本文件记录通过 WebBridge 爬取一个微信公众号全部最新文章时遇到的典型问题和解决方式。

## 1. 搜狗微信搜索 type=2 的搜索语义

### 重要：type=2 是内容搜索，不是作者搜索

搜狗微信 `type=2`（文章搜索）是通过 **文章全文内容** 进行关键词匹配的，**不是**按公众号作者名过滤。这意味着：

- 搜索公众号昵称（如"艾瑞咨询"）返回的文章中，大部分可能来自其他公众号，只是文章内容中提到了"艾瑞咨询"这个关键词
- 真正的「艾瑞咨询」公众号自己发布的文章，在搜狗结果中占比可能很小

### 正确提取方法

通过 `.s-p` (来源元素) 中的文本判断哪些结果真正来自目标公众号：

```javascript
(function() {
  var results = [];
  document.querySelectorAll('ul.news-list > li').forEach(function(li) {
    var titleEl = li.querySelector('h3 a');
    var linkEl = li.querySelector('.img-box a[data-z="art"]');
    var sourceEl = li.querySelector('.s-p');
    var source = sourceEl ? sourceEl.textContent.trim() : '';
    // 文本格式: "小伊评科技document.write(timeConvert('1710990148'))2024-3-21"
    var realSource = source.replace(/document\.write\([^)]+\)/g, '').trim();
    if (titleEl && linkEl && realSource.indexOf(targetNickname) !== -1) {
      results.push({title: titleEl.textContent.trim(), sogou_url: linkEl.href, source: realSource});
    }
  });
  return JSON.stringify(results, null, 2);
})()
```

### 搜狗链接是相对路径

从搜狗页面提取的链接格式为 `/link?url=...`，需要用 `https://weixin.sogou.com` 前缀拼接为绝对 URL：

```javascript
var fullUrl = href.startsWith('http') ? href : 'https://weixin.sogou.com' + href;
```

### 搜狗链接重定向

访问搜狗链接 `https://weixin.sogou.com/link?url=...` 会：
1. 302 重定向到 `mp.weixin.qq.com/s?...`（带 src/ver/signature 参数）
2. 这个 URL 可以直接用 `skill_webbridge_navigate` 打开（自动跟随 302）
3. 打开后页面标题即为文章标题，可通过 `#js_content` 提取正文

## 2. HTTP 搜狗 antispider

Python `requests` 直接访问搜狗微信时：

- 返回 HTTP 200 但页面不含搜索结果（被静默替换为验证页）
- `"antispider" in resp.url.lower()` 关键词检测可能漏报 — 验证页不总是包含这个关键词
- URL 可能被截断（如 `ie` 结尾）作为 antispider 信号
- 唯一可靠的绕过方式是使用 WebBridge（用户的真实浏览器环境）

**不受 antispider 影响的**：通过 WebBridge navigate 到搜狗 URL，因为使用的是用户真实浏览器的 IP、Cookie、UA。

## 3. 种子文章违规/不可见

### 问题

当 seed URL 的文章被删除/显示"此内容因违规无法查看"时：
- 链式发现 (chain discovery) 从该页面提取不到任何链接
- `crawl_new_articles()` 调用 `extract_links_from_page(seed_url)` 返回 0
- 脚本输出 `没有新文章`

### 解决方案

1. 通过搜狗搜索 `type=2 & query=公众号昵称` 找到真实文章
2. 用 WebBridge navigate 到搜狗结果 → 自动 302 到文章 URL
3. 提取并保存文章
4. 手动 populate `data/{site_id}/seen_urls.json`，包含找到的真实文章 URL
5. 后续链式发现就能从已有文章页面中提取新链接

## 4. 引导链式发现 (Bootstrap Chain Discovery)

对于新站点，首次需要手动引导：

1. **手动找到种子文章**：WebBridge 搜狗搜索 → 导航 → 提取内容 → 入库
2. **手动写 seen_urls**：将找到的 URL 写入 `data/{site_id}/seen_urls.json`
3. **运行脚本**：`python3 scripts/wechat_{site_id}_crawl.py`
4. 脚本会从 seen_urls 中的文章页面提取链接，发现新文章

### seen_urls.json 格式

```json
[
  "https://mp.weixin.qq.com/s?src=11&timestamp=...&signature=...",
  "https://mp.weixin.qq.com/s/UMpDoV0xIZZmEN3Z-G_BMw"
]
```

注意：被删除的种子文章也建议放入 seen_urls，避免重复发现已失效的 URL。

## 5. 常见问题速查

| 现象 | 原因 | 处理 |
|------|------|------|
| 搜狗搜索返回 0 条 | HTTP antispider | 用 WebBridge |
| WebBridge 搜到文章但来源不对 | type=2 是内容搜索 | 检查 `.s-p` 来源字段 |
| 链式发现返回 0 条 | seed 文章已删除 | 手动找到真实文章并写入 seen_urls |
| 入库成功但后续脚本找到 0 条 | seen_urls 未更新 | 检查脚本 `save_seen` 逻辑 |
| 搜狗结果中日期格式含 script | `document.write(timeConvert(...))` | 用正则去掉，或从页面源代码 `var ct=` 提取时间戳 |
