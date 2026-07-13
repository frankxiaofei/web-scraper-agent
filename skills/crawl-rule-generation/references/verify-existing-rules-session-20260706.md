# 验证已有规则的本次会话实践 (2026-07-06)

## 背景

用户要求补充/确认四个站点的BIM爬取规则，并删除重复定时任务。涉及站点：
1. 大唐 cdt-ec.com
2. 三峡 eps.ctg.com.cn
3. 国电投 ebid.espic.com.cn
4. 华电 chdtp.com (重复删除)

## 操作过程

### 第一步：全局状态快照

```python
# 1. 获取所有站点状态
crawl_list_sites()  # 19个站点, 14 enabled
# 2. 获取所有定时任务
cronjob(action='list')  # 10个定时任务
# 3. 对每个站点读规则
crawl_get_rule(site_id='中国大唐集团有限公司_大唐集团电子商务平台')
crawl_get_rule(site_id='中国长江三峡集团有限公司_电子采购平台')
crawl_get_rule(site_id='国家电力投资集团有限公司_国家电投电子商务平台')
```

### 发现

**重复定时任务**（通过cronjob列表发现）：
- 华电chdtp: 任务ID `fce7aa7a03f2` 和 `b6ff6f8ae39c` 都是4:00执行BIM爬取
- 华能ec.chng: 任务ID `81fe104c7d6c` 和 `5dad3498285e` 都是4:30执行BIM爬取

### 大唐cdt-ec.com 验证

**规则概况**：
- entry_url: `https://www.cdt-ec.com/notice/moreController/toMore?globleType=0`
- list_page: strategy=api, url=`/notice/moreController/getList`, POST
- search: enabled=true, api_body_override 包含 `message_title=BIM`
- detail: url_template=`/notice/moreController/moreall?id={link}`, content_selector=body

**验证发现**：
1. API getList 有阿里云WAF (acw_sc__v2)，直接HTTP请求返回WAF验证页面
2. Hermes Browser navigate到toMore页成功，页面使用layui table渲染
3. 在搜索框输入BIM → 点击搜索 → 浏览器实际跳转到 `/home/cwemeAppDownLoad.html`（App下载页面）
4. 说明表单默认提交行为未禁用，搜索必须走API方式

**详情页验证**：
- 通过Hermes browser点击公告列表项进入详情，URL格式 `/cms/channel/1ywgg1/{数字id}.htm`
- 详情页内容完整可读，无登录墙
- .article-content 选择器有效

### 三峡eps.ctg.com.cn 验证

**规则概况**：
- entry_url: `https://eps.ctg.com.cn/cms/channel/1ywgg1/index.htm`
- list_page: strategy=dom, container=#list1, item=li[name='li_name']
- search: type=webbridge_interactive, 步骤为 navigate → fill #inp-txt → click #btnSearch
- detail: content_selector=.article-content

**验证发现（2026-07-06 实际测试）**：
- 详情页无需登录，HTTP直连可访问（200），content_selector `.article-content` 验证有效
- 搜索框 `#inp-txt` 存在，搜索按钮 `#btnSearch` 存在（验证通过）
- 搜索后实际跳转到 `/cms/search.htm?kwd=BIM&channelIds=204%2C210%2C...`（非 `/search.jspx`）
- 搜索结果显示"当前搜索到 58 条与'BIM'相关内容"，共6页（2026-07-06数据）
- 搜索结果列表使用 **与普通列表页相同的DOM结构**：`li[name='li_name']`，可被现有list_page规则解析
- 搜索结果的列表通过 AJAX（search.js）动态加载，curl初始HTML显示0条，WebBridge渲染后显示58条
- 详情URL格式：`/cms/channel/1ywgg1/{数字id}.htm`，与普通列表一致
- **规则充分**：search部分WebBridge交互可行，结果列表复用已有list_page DOM解析，详情HTTP直连可行

### 国电投ebid.espic.com.cn 验证

**规则概况**：
- entry_url: `https://ebid.espic.com.cn/newgdtcms/category/bulletinListNew.html?dates=300&categoryId=2&tenderMethod=01&tabName=招标信息&page=1`
- list_page: strategy=dom_after_ajax
- search: type=webbridge_interactive, 有WAF标注

**问题**：
- 规则是占位风格，selector过于宽泛（`tr, li, .bulletin-item`）
- 详情content_selector同样宽泛（`#detailContent, .main-content` 等）
- 需要实际测试才能确认

## 关键经验

1. **先crawl_list_sites + cronjob列表** — 快速了解全局状态和重复任务
2. **搜索按钮点击后检查页面是否跳转** — document.location.href 确认是否原地过滤
3. **API WAF阻断时通不过HTTP** — 走WebBridge浏览器环境
4. **两个任务同名同schedule是重复** — cnronjob列表一眼能看出来
