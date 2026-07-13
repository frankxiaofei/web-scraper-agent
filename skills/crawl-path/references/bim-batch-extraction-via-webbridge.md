# BIM 批量公告提取 — WebBridge + delegate_task

实际会话：2026-06-29，tjbid → dlzb.com 搜索 BIM → 保存 20 条公告

## 场景

用户要求从 `https://tjbid.dlzb.com/v1/` 爬取 BIM 招标公告。但该子站公告列表不包含 BIM 内容（全是物资采购/工程分包），搜索框重定向到父站 `www.dlzb.com`。

## 全流程记录

### 步骤 1：WebBridge 搜索

```javascript
// navigate → fill 搜索框 "BIM" → 点击搜索按钮
skill_webbridge_navigate(url="https://tjbid.dlzb.com/v1/")
skill_webbridge_fill(selector="@e6", value="BIM")
skill_webbridge_click(selector="@e7")
// 页面重定向到 https://www.dlzb.com/search/?kw=BIM
```

### 步骤 2：JS 提取 20 条含 BIM 的公告

```javascript
(() => {
  const links = document.querySelectorAll('a[href*="d-zb-"]');
  let results = [];
  links.forEach(a => {
    const text = a.textContent.trim();
    if ((text.includes('BIM') || text.includes('bim')) && !results.some(r => r.url === a.href)) {
      results.push({title: text, url: a.href, date: ''});
    }
  });
  return JSON.stringify(results.slice(0, 20), null, 2);
})();
```

### 步骤 3：delegate_task 分批次保存

分 X 轮（每轮 3 条），每轮一个 delegate_task：

```json
{
  "tasks": [
    {"goal": "保存BIM公告：browser_navigate(URL) → 提取正文 → skill_save_notice(...)",
     "toolsets": ["browser", "crawl-skills"]},
    ...
  ]
}
```

### 步骤 4：父代理兜底

子代理返回后，检查 `tool_trace` — 找到有 `skill_save_notice` status=ok 的已保存；
其余手动调用 `skill_save_notice(site_id='dlzb_power', title=..., url=..., publish_date=..., content_text=...)`

### 步骤 5：验证

```bash
crawl_query_notices(site_id="dlzb_power", keyword="BIM", per_page=25)
```

## Detail 页面内容提取（dlzb.com 会员模式）

dlzb.com 的公告详情页（`d-zb-*`）有两个内容层：

1. **AI导读区**（非会员可见）— 结构化的招标概要，含项目名称、地点、资质要求、时间节点等
2. **全文正文区**（需银牌以上会员）— 完整的公告正文，在 `#content`、`.content`、`.detail-content` 等容器中

### 提取脚本

```javascript
// 推荐：一次性提取两个区域
(() => {
  const el = document.querySelector('#content, .content, .detail-content, .detail, .pdbox, .content_main');
  return el?.innerText || document.body.innerText.substring(0, 4000);
})();
```

AI导读区通常包含：
- 项目基本信息：名称、编号、招标人、资金来源
- 项目概况：规模、地点、服务期限
- 投标人资格要求：资质、业绩门槛
- 时间节点：公告发布、投标截止、开标时间
- 联系方式：联系人、手机、邮箱、电话

全文正文区包含完整章节（公开招标时可见）：
- 招标条件、项目概况与招标范围、投标人资格要求
- 招标文件获取方式、投标文件递交要求
- 发布公告的媒介、联系方式、监督机构

### delegate_task 子代理详情提取模板

```json
{
  "goal": "访问 https://www.dlzb.com/d-zb-XXXXXXX.html，用 JavaScript 提取页面正文：document.querySelector('#content, .content, .detail-content, .detail, .pdbox, .content_main')?.innerText。返回提取到的完整详情文本，包含项目名称、投标截止时间、资格要求等关键信息。",
  "context": "你正通过已登录的 WebBridge 浏览器访问 dlzb.com。页面可能包含会员专区遮挡，但 AI 导读区可见。",
  "toolsets": ["browser"]
}
```

### navigate + evaluate 注意

- 用 `skill_webbridge_evaluate` 执行 `window.location.href = '...'` 来导航**不生效**（async 限制）
- 正确的做法：`skill_webbridge_navigate(url=...)` 先导航，然后 `skill_webbridge_evaluate` 提取
- evaluate 代码必须是**同步** IIFE：`(() => { ... })()` — 不能用 async/await

## 数据样例

20 条公告的标题和 URL 记录在父会话中。关键特征：
- 来源：`www.dlzb.com`（电力招标网全站）
- 站点 ID：`dlzb_power`
- 日期范围：2026-06-17 至 2026-06-29
- 系统自动 BIM 标记：是（is_bim_related=true, 置信度 0.9-1.0）

## 子代理不保存时的恢复方法

如果子代理 task 声称 "skill_save_notice not available"，从子代理的 `summary` 中提取：
- publish_date（页面显示的 "日期:YYYY-MM-DD"）
- AI导读正文摘要
- 联系人信息

然后父代理手动构造保存参数即可。不需要重新 delegate。
