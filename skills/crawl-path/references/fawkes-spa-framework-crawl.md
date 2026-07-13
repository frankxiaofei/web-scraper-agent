# Fawkes SPA Framework 爬取笔记

## 概述

华电（华东院）数字档案馆 `http://da.hdec.com` 使用 **Fawkes Runtime Framework**（亦称浙大/华电自研 Web 组件框架）。该框架的核心特征：

- 应用是完全的 **Web Components** 架构（Custom Elements + Shadow DOM）
- 页面路由通过 **hash fragment** 控制（`#/application/{appId}/worksheet/{wsId}`）
- 框架核心 JS 文件：`fawkes-runtime-framework.js`
- 内容渲染在 Shadow DOM 内部，外部 `document.querySelector` 无法访问

## 页面特征

### 登录状态
- SSO Token 存储在 Cookie（`ssoToken` JWT）
- 页面标题栏显示当前用户名（如"李"）
- 但 SPA 内额外的 `fetch()` / `XMLHttpRequest` 调用会返回 `{"status":false,"code":-8000140,"message":"未登录，无权限访问"}`——SSO 认证可能依赖首次页面加载建立的安全上下文，后续 JS 发起的 API 请求缺少必要签名/token

### 菜单结构
- 顶部档案分类菜单：工程图纸、工程档案、科研档案、公文档案、合同档案、荣誉档案、声像档案、标准规范、科技图书、科技资料、政策法规、规章制度、采购档案
- 顶部功能菜单：首页、跨库检索、工程中心、数据集、知识库、表单中心、个人中心
- 当前用户显示：华东院 / 用户平台
- 页面底部："仅限院内员工参考，不得对外提供，不得用于任何商业用途。"

### 路由行为
- `#/application/{applicationId}/worksheet/{worksheetId}` 可能被 SPA 框架重定向到默认页面（如标准规范首页）
- hash fragment 设置后框架不一定会响应路由变化（可能 rAF/onhashchange 监听了但框架内部有条件判断）
- 设置 `window.location.hash = ...` 也不会触发切换

## 数据提取方法

### 方案 A：`document.body.innerText`（最简）

此方法绕过 Shadow DOM 限制，直接读取渲染后的可见文本。适用于提取列表内容、搜索结果等。

```javascript
// 获取页面所有可见文本
document.body.innerText
```

**示例输出**（标准规范列表页）：
```
标准编号 | 标准名称 | 有效情况 | 实施日期 | 替代编号 | 废止日期 | 库存数
GB/T7064-2017 | 隐极同步发电机技术要求 | 有效 | 2018-07-01 | | | 0/0
SL/Z552-2012 | 用水指标评价导则 | 废止 | | | 2025-11-05 | 0/0
...
总份数 33,526 | 电子文件数 37,242
```

### 方案 B：Performance API 发现端点

```javascript
// 发现所有 API 请求
performance.getEntriesByType('resource')
  .filter(r => r.initiatorType === 'xmlhttprequest' || r.initiatorType === 'fetch')
  .map(r => r.name)
```

**已知端点**（da.hdec.com 域）：
- `/api/sign/ts` — 时间戳签名
- `/api/sys-system/clientInfo` — 客户端信息
- `/api/sys-user/user/portals` — 门户列表
- `/api/sys-user/user/menus` — 菜单树
- `/api/sys-user/currentUserInfo` — 当前用户信息
- `/api/sys-form/form/filter?type=1&id={worksheetId}&affiliatedLibrary=usage` — 表单筛选（被 SSO 阻止）
- `/api/sys-form/list/config?id={worksheetId}&affiliatedLibrary=usage` — 列表配置（被 SSO 阻止）
- `/api/sys-storage/download_image?f8s={hash}` — 图片/文件下载
- `/api/da-management/ai/token/new` — AI token

### 方案 C：访问 accessibility tree

WebBridge snapshot 返回完整的 accessibility tree（含 Shadow DOM 内容），可通过 `skill_webbridge_snapshot` 读取页面中可见的交互元素和文本。

### 方案 D：Screenshot

`skill_webbridge_screenshot` 可获取页面截图（用户已在浏览器中登录的情况下，能看到完整页面内容）。

## 不可行的方案

### fetch/XHR 调用 API

即使页面已登录（Cookie 中有 `ssoToken`），从 `skill_webbridge_evaluate` 发起的请求都会返回"未登录"：

```javascript
// 这行在 SPA 上下文中执行——失败
fetch('/api/sys-form/form/filter?type=1&id=1784509371813572610&affiliatedLibrary=usage', {
  credentials: 'include'
})
// 返回: {"status":false,"data":null,"code":-8000140,"message":"未登录，无权限访问"}
```

### 外部 curl

从非浏览器环境（外部服务器）直接请求 da.hdec.com 完全不通（内网/VPN 隔离）。

## 局限性

1. **仅限页面可见内容提取** — 无法获取需点击展开的详情、附件下载链接等
2. **无法进行 API 级操作** — SSO 安全上下文绑定在初始页面加载，额外请求无法认证
3. **路由失效** — `worksheet` 路由被重定向，无法通过 URL 参数直接定位到目标工单
4. **无 iframe / Shadow DOM 遍历** — 框架将内容渲染在 Shadow DOM 内，`querySelector` 找不到

## 适用场景

- ✅ 提取当前页面列表/搜索结果文本
- ✅ 截图获取页面快照（用户可见的完整内容）
- ❌ 文件/附件下载（需要 API 权限）
- ❌ 导航到指定工单/表单页面（路由被重定向）
