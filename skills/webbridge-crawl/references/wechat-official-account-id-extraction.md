# 微信公众号微信号提取

## 目标

从一篇微信公众号文章的公开链接（`mp.weixin.qq.com/s/...`）中提取该公众号的微信号（`user_name`，格式如 `gh_xxxxxxxxxxxx`）。

有了微信号，后续可以通过搜狗微信等渠道搜索该公众号的历史文章。

## 方法：curl + grep 提取原始 HTML

微信公众号的页面源码中嵌有包含公众号信息的 JavaScript 变量，通过 curl 获取原始 HTML 后可直接 grep 提取。

### 步骤

```bash
# 1. 获取页面源码，只提取关键变量
curl -s 'https://mp.weixin.qq.com/s/kMbYVX6KgoWPeYc1m2jnzA' \
  -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36' \
  | grep -oE '(user_name|nick_name|fakeid|__biz|round_head_img)[":= ]+[^"& ]+'
```

### 输出示例

```
biz: "MzI2MDMxMTA5OQ=="
__biz=${window.biz}
user_name: 'gh_3d4e3a3f19bf',
nick_name: '浩鲸科技',
round_head_img: 'http://mmbiz.qpic.cn/...',
biz: false,
```

### 关键字段

| 变量 | 含义 |
|------|------|
| `user_name` | 微信号（`gh_xxxxxxxxxxxx`），最常用 |
| `nick_name` | 公众号名称 |
| `__biz` / `biz` | 公众号唯一业务 ID（Base64 编码），可用于构造历史文章请求 |
| `round_head_img` | 公众号头像 URL |

## 注意

- 必须使用真实的浏览器 User-Agent（微信页面检测非浏览器 UA 会返回空内容或验证页）
- `User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36` 即可
- 无需 Cookie 或登录态——公众号文章页面是公开可访问的
- 如果 curl 返回空或验证码页面，改用 `web_extract` 或 `browser_navigate` 后通过 Console JS 提取（但微信公众号对 headless 浏览器限制较严，curl 反而更可靠）

## 为什么不直接用浏览器的 JS 变量

微信公众号的页面保护机制会阻止在浏览器 Console 中直接读取 JS 变量（`window.__biz` 等）。原始 HTML 中这些变量是硬编码在 `<script>` 标签中的字符串，不受保护。

## 后续：获取历史文章列表

有了微信号（`user_name`）后，可以通过搜狗微信搜索获取历史文章：

1. 访问 `https://weixin.sogou.com`
2. 搜索公众号名称或微信号
3. 进入公众号主页查看历史文章列表
4. 或直接利用 `__biz` 参数构造微信的 profile URL：
   ```
   https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=MzI2MDMxMTA5OQ==&scene=110
   ```
   （需携带 Cookie，登录态下可查看全部历史文章）
