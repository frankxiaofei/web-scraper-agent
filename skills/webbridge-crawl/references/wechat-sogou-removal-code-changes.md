# 公众号爬取代码改造：搜狗 HTTP → WebBridge 代码级迁移

## 背景

2026-07-09：用户要求全部公众后爬取不再使用搜狗微信 HTTP 搜索（antispider 阻挡），默认使用 WebBridge。

## 根因链

1. `src/core/wechat_crawl.py` 中的 `search_via_sogou()` 使用 Python requests 访问 `weixin.sogou.com/weixin?type=2`
2. 搜狗检测到 Python UA/IP 特征，返回 antispider 验证页（状态码200但无搜索结果）
3. `discover_article_urls()` 和 `crawl_new_articles()` 均默认 `use_sogou=True`
4. 所有 `scripts/wechat_*.py` 脚本传入 `use_sogou=not args.chain_only`（即默认启用搜狗）
5. `src/adapters/wechat.py` 也走同一路径
6. 链式发现 `chain_discovery()` 使用 HTTP 对已知文章提取链接，但微信文章页的「最新动态」区域由 JS 异步加载，HTTP 请求抓不到
7. 结果：所有公众号爬取永久返回 0 条

## 已做修改

### `src/core/wechat_crawl.py`

- `search_via_sogou()` → 标记为已弃用，直接 `return set()`
- 新增 `search_via_webbridge(nickname, max_pages=2, session_id=None)`: 
  - 直接通过 HTTP POST 到 `KIMI_WEBBRIDGE_URL`（默认 `http://127.0.0.1:10086/command`）
  - 用 `navigate` 打开搜狗搜索页 → `evaluate` 提取 `.news-list > li` → 对每条结果再次 `navigate` 跟 302 → 提取 `mp.weixin.qq.com/s/` 真实 URL
  - 需要添加 `import os` 以读取环境变量
- `discover_article_urls()` → 默认改为 `use_sogou=False, use_webbridge=True`
- `crawl_new_articles()` → 默认改为 `use_sogou=False, use_webbridge=True`, 新增 `use_webbridge` 参数透传

### `src/adapters/wechat.py`

- `fetch_list()` 调用 `crawl_new_articles()` 时传 `use_sogou=False, use_webbridge=False`（定时同步在 headless 后端，无 WebBridge）

### `scripts/wechat_*.py`（共7个文件）

所有调用 `crawl_new_articles()` 的地方从：
```python
use_sogou=not args.chain_only,
use_chain=not args.sogou_only,
```
改为：
```python
use_sogou=args.sogou_only if hasattr(args, 'sogou_only') else False,
use_webbridge=not getattr(args, 'chain_only', False),
use_chain=not getattr(args, 'sogou_only', False),
```

### Cron 任务（共8个）

所有 cron job 的 prompt 中脚本调用改为 `--chain-only` 模式（cron 在 headless 环境运行，无 WebBridge 可用）：
```
python3.11 scripts/wechat_xxx_crawl.py --max-articles 15 --chain-only
```

## 定时任务 vs 手动任务的分工

| 场景 | 发现方法 | 说明 |
|------|---------|------|
| Cron 定时（每日8点） | `--chain-only` | headless 环境，只从 seen_urls 中已有文章页提取链接 |
| Hermes 对话手动爬取 | use_webbridge=True（默认） | 使用用户真实浏览器 WebBridge 执行搜狗搜索 |
| 首次引导 | 手动 WebBridge | 需要用户手动执行一次搜狗搜索，找到种子文章并入库，建立 seen_urls |

## 链式发现的局限性

即使通过 WebBridge navigate 到微信文章页，`document.querySelectorAll('a[href*="mp.weixin.qq.com/s"]')` 也可能返回空数组。原因：
- 微信文章页底部的「最新动态」区域通过 JS 异步加载，可能延迟出现
- 某些文章（如老文章、违规文章）根本不显示「最新动态」
- 页面滚动后才加载推荐内容（需要 scroll 触发）

解决方案：
1. 用 WebBridge navigate 到种子文章 → evaluate 搜索链接
2. 如果搜不到，用搜狗搜索找到新文章 URL 手动入库
3. 后续 cron 的 `--chain-only` 只能从已有文章中提取链接，新文章需要搜狗补充

## `search_via_webbridge()` 实现要点

```python
def search_via_webbridge(nickname, max_pages=2, session_id=None):
    sid = session_id or f"wechat-{nickname}-{int(time.time())}"
    daemon_url = os.environ.get("KIMI_WEBBRIDGE_URL", "http://127.0.0.1:10086")
    
    for page in range(1, max_pages + 1):
        # 1. navigate 到搜狗搜索页
        req.post(f"{daemon_url}/command", json={
            "command": "navigate",
            "arguments": {"url": sogou_url, "new_tab": page==1, "session_id": sid}
        })
        time.sleep(3)
        
        # 2. evaluate 提取列表
        req.post(f"{daemon_url}/command", json={
            "command": "evaluate",
            "arguments": {"code": "...JS to extract ul.news-list..."}
        })
        
        # 3. 对每条搜狗链接再 navigate（跟302到真实 mp.weixin.qq.com URL）
        for item in items:
            req.post(f"{daemon_url}/command", json={
                "command": "navigate",
                "arguments": {"url": item["url"], "session_id": sid, "new_tab": False}
            })
    
    # 4. 关闭 session
    finally:
        req.post(f"{daemon_url}/command", json={
            "command": "closeSession", 
            "arguments": {"session_id": sid}
        })
```

**注意**：这个函数直接调 WebBridge daemon API，不是通过 `skill_webbridge_*` 工具，所以可以在 Python 脚本中运行。但只能在本机（daemon 所在机器）执行。
