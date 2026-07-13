# 企业内部 SPA 数字档案馆 — WPS WebOffice 文件预览模式

## 站点特征

部分企业（如华东院 da.hdec.com）部署了内部的数字档案馆系统，其特征包括：

- **前端框架**：Vue 3 + Element Plus（custom 组件前缀 `fks-*`）
- **路由模式**：Hash 路由 `#/application/{appId}/worksheet/{worksheetId}`
- **Tab 系统**：页面上方有 tags-view（标签页），可打开多个模块
- **登录态**：localStorage + Bearer token（`production_digital-archives_access_token`）
- **数据获取**：通过后端 API（`/api/sys-form/form/filter`、`/api/sys-form/list/config` 等）加载

## 页面结构

### 列表页

列表使用自定义表格组件（`.fks-table__row`），关键列：

| 列 | 内容 | 是否可点击 |
|----|------|-----------|
| 复选框 | row 选择 | 是（`<label class="fks-checkbox">`） |
| 序号 | 数字 | 否 |
| 电子文件(图标) | 文件图标 | 是（点击打开详情对话框） |
| 标准编号 | 如 GB/T7064-2017 | 是（`<a class="fks-link fks-link--primary">`） |
| 标准名称 | 如 隐极同步发电机技术要求 | 是 |
| 库存数 | 如 `3/3`（库存/可下载） | 否 |

`3/3` 表示：3份纸质库存/3份电子文件（即可在线查看下载）

### 详情对话框

点击行内任意链接打开两个重叠对话框：

1. **标准编目对话框**（`.fks-overlay-dialog`）：显示标准元数据表单（标准编号、全宗号、馆藏号、名称、分类号、语种、有效期、编制单位等）
2. **电子文件列表**（在标准编目对话框内 `#detail-table_detail_documents`）：显示该标准关联的电子文件

### 文件预览对话框

点击电子文件列表中的文件名（`<a class="fks-link">`）打开：

- **文件预览对话框**（`.new-preview-dialog`）：标题显示"文件预览"
- **左侧文件列表**（`.file-box.file-box-active`）：显示文件名 `【1】GBT+7064-2017.pdf` 并带缩略图
- **右侧 WPS 预览区域**：通过 `<iframe.id="office-iframe">` 加载内网 WPS 服务

## WPS WebOffice 预览

文件预览通过企业内部 WPS 服务实例：

```html
<iframe id="office-iframe" 
  src="http://10.215.148.83/weboffice/office/f/{file_id}?_w_appid=...&_w_third_file_id=...&_w_third_username=...&readonly&wpsPreview=0000000">
</iframe>
```

特征：
- WPS 服务部署在企业内部网络（10.x.x.x）
- 参数含 `readonly` 和 `wpsPreview=0000000`（非编辑模式）
- 预览通过 iframe 嵌入，WPS 工具栏在 iframe 内部（不可通过父页面 JS 直接操作）

## 文件下载方法

### 方式 1：浏览器直接交互（推荐）

在用户已登录的浏览器中：

1. **点击标准编号/名称** → 弹出详情对话框
2. **滚动到电子文件列表**（通常在详情底部）
3. **点击文件名**（`.fks-link` 中的 PDF/文件名链接）→ 弹出文件预览对话框
4. **查看 WPS iframe 预览工具栏** — 部分 WPS 版本提供下载按钮
5. **右键预览区域 → 另存为** 也可下载

### 方式 2：文件 API（需要 token/Cookie）

```
GET /api/sys-storage/file?id={file_id}
```

- 需要有效的会话 Cookie/Token
- 通过纯 XHR 调用返回 401（请求未携带正确鉴权）
- 可通过浏览器地址栏直接访问（利用已登录的 Cookie）
- 缩略图 API：`GET /api/sys-storage/down_thumbnail?f8s={hash}&thumbnail=true&width=170&height=280`

### 方式 3：WPS 预览 URL

WPS iframe src URL 中的 `file_id` 参数可尝试直接访问：
```
http://10.215.148.83/weboffice/office/f/{file_id}?readonly&wpsPreview=0000000
```
但这只能从内网访问。

## 通过 WebBridge 下载的工作流

```javascript
// Step 1: 导航到列表页
skill_webbridge_navigate(url="http://da.hdec.com/#/application/{appId}/worksheet/{worksheetId}")

// Step 2: 点击标准编号链接打开详情
document.querySelectorAll('.fks-link__inner')[0].click();

// Step 3: 等待对话框加载
// (对话框以 overlay 形式弹出)

// Step 4: 在电子文件列表中查找文件名链接并点击
document.querySelector('#detail-table_detail_documents .fks-link').click();

// Step 5: 文件预览对话框弹出，WPS iframe 加载
// 此时在用户浏览器上可见文件内容

// Step 6: 获取文件 ID 用于下载
// 从 iframe src 或从 network API 请求中提取
var fileId = document.getElementById('office-iframe')?.src.match(/file\/([^?]+)/)?.[1];
// 或在浏览器新标签中打开: http://da.hdec.com/api/sys-storage/file?id={fileId}
```

## 分页识别

列表底部有分页控件，显示 `共 {N} 条` 和页码。

## 注意事项

1. **文件预览需要用户浏览器环境**：WPS iframe 中的下载功能无法通过自动化工具触发
2. **下载限制**：部分企业配置了文件下载权限（仅允许预览不允许下载），需注意
3. **缩略图API**：`GET /api/sys-storage/down_thumbnail?f8s={hash}&thumbnail=true` 可获取预览缩略图
4. **类型识别**：表格的"电子文件"列图标可能存在但有文件数=0（`0/0`），表示无电子文件可供查看
5. **库存数与电子文件数不同**：`3/3` = 3份纸质复本 / 3个电子文件可访问
