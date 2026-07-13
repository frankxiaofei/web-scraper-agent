# openstd.samr.gov.cn — 强制性国家标准爬取说明

## 站点概况

国家市场监督管理总局「国家标准全文公开系统」的强制性国家标准频道。共 **6120 条** 标准，分 **612 页**（每页10条），按流通日期倒序排列。

## URL 结构

| 功能 | URL | 参数 |
|------|-----|------|
| 列表页 | `https://openstd.samr.gov.cn/bzgk/std/std_list_type` | `p.p1={page}`（页码）, `p.p90=circulation_date`, `p.p91=desc` |
| 详情页 | `https://openstd.samr.gov.cn/bzgk/std/newGbInfo` | `hcno={hcno}` |
| 在线预览 | `https://openstd.samr.gov.cn/bzgk/std/showGb` | `type=online`, `hcno={hcno}`, `request_locale=zh` |
| 下载 | `https://openstd.samr.gov.cn/bzgk/std/showGb` | `type=download`, `hcno={hcno}`, `request_locale=zh` |

## 列表页

- **CSS 容器**: `table.table.result_list.table-striped.table-hover`
- **行选择器**: `tr:not(:first-child)`（跳过表头）
- **列**:
  1. 序号 (seq)
  2. 标准号 (standard_no) — `<a onclick="showInfo('HCNO')">`
  3. 是否采标 (caibiao) — 含 `<span class="label label-warning">采</span>` 或空白
  4. 标准名称 (std_name) — `<a onclick="showInfo('HCNO')">`
  5. 状态 (status) — 现行/即将实施/废止
  6. 发布日期 (publish_date)
  7. 实施日期 (implement_date)
  8. 操作 — 含"查看详细"链接
- **hcno 提取**: 从 `onclick="showInfo('HCNO')"` 属性正则 `'([A-F0-9]+)'`
- **翻页**: URL 参数 `p.p1=N`，直接修改 URL 即可，无需 JS 点击
- **统计**: 6120 条 / 612 页

## 详情页

详情页 `newGbInfo?hcno=XXX` 可直接 HTTP GET（无需浏览器登录），返回包含完整元数据的 HTML：

| 字段 | HTML 提取方式 |
|------|--------------|
| 标准号 | `标准号：GB XXXX-2026` |
| 中文名称 | `中文标准名称：XXX` |
| 英文名称 | `英文标准名称：XXX` |
| 状态 | `标准状态： 现行/即将实施/废止` |
| CCS | `中国标准分类号（CCS）XXX` |
| ICS | `国际标准分类号（ICS）XX.XXX.XX` |
| 发布日期 | 表格字段 |
| 实施日期 | 表格字段 |
| 主管部门 | 工业和信息化部等 |
| 归口部门 | 工业和信息化部等 |
| 发布单位 | 国家市场监督管理总局、国家标准化管理委员会 |
| 在线预览 | 按钮 `.ck_btn`（文本"在线预览"），调用 `showGb(type='online', hcno)` |

## 在线预览 PDF

PDF 预览通过 `window.open` 在新窗口打开 `showGb?type=online&hcno=XXX`。有 referrer 检查。**推荐的打开方式**：

1. 在详情页用 skill_webbridge_evaluate 执行：
```javascript
window.open = function(url, name, features) {
    window.location.href = url;
    return window;
};
document.querySelector('button.ck_btn').click();
```
2. 等待 3-5 秒让 PDF.js 加载
3. 确认 `document.title` 含 "在线预览"
4. 截图：`skill_webbridge_screenshot`
5. 翻页：`document.getElementById('next').click()`
6. 总页数：`document.getElementById('numPages').textContent`

详见 [image-based-pdf-preview.md](image-based-pdf-preview.md)。

## 爬取脚本

路径：`scripts/crawl_openstd_standards.py`

支持两种模式：
- **阶段一（HTTP）**: 列表爬取 + 详情元数据
- **阶段二（WebBridge）**: PDF 预览截图

```bash
# 爬取前 N 页（含详情元数据）
python scripts/crawl_openstd_standards.py --max-pages 3

# 截图指定标准
python scripts/crawl_openstd_standards.py --hcno 524FDF24793BCC6D2BE9CDA52A418114

# 从已爬取列表批量截图
python scripts/crawl_openstd_standards.py --screenshot --max-items 5

# 全量爬取 612 页
python scripts/crawl_openstd_standards.py --all
```
