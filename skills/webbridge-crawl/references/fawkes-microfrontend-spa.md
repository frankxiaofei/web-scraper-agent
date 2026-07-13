# Fawkes / qiankun 微前端 SPA 导航

## 背景

华东院数字档案馆（da.hdec.com）使用 **Fawkes Runtime Framework**（基于 qiankun 微前端架构）。这是一个内部企业系统，URL 使用 hash 路由：
```
http://da.hdec.com/#/application/{appId}/worksheet/{worksheetId}
```

## 已知问题：深层链接路由不匹配

**症状**：WebBridge navigate 到 `#/application/{appId}/worksheet/{worksheetId}` 后，SPA 加载了默认模块（如"标准规范"），而不是指定的 worksheet。

**原因**：Fawkes Runtime Framework 加载微前端模块有两种方式：
1. 通过用户点击左侧菜单（如"表单中心"）→ 加载对应的 app
2. 通过 hash 路由直接加载 — 但这需要微前端子模块已经在运行时中注册

当 navigate 直接到深层链接时，Fawkes 框架可能：
- 找不到对应 app 的注册信息（因为子模块懒加载）
- 回退到默认的 dashboard/首页模块
- 不触发路由匹配

## 排查方法

### 1. 确认登录状态

检查 localStorage 中是否有用户信息和 token：
```javascript
Object.keys(localStorage).filter(k => k.includes('token') || k.includes('user') || k.includes('auth'))
```

### 2. 确认框架运行时版本

查看页面加载的脚本：
```javascript
document.querySelectorAll('script[src*="fks"]').forEach(s => console.log(s.src))
```
Fawkes 框架的入口通常是 `fawkes-runtime-framework.js` + `qiankun-entry.js`。

### 3. 检查当前页面实际内容

```javascript
document.body.innerText.substring(0, 2000)
```
对比 hash 路由期望的内容与实际渲染内容。

### 4. 检查 DOM 容器

Fawkes 通常渲染到 `#fks-app` div 中，内部子模块通过 qiankun 独立渲染：
```javascript
document.getElementById('fks-app')?.children?.length
document.getElementById('app')?.children?.length
```

## 解决方法

### 方法 A：通过用户交互导航（推荐）

1. navigate 到系统首页（不带 hash 路由或只带 `#/`）
2. 通过左侧菜单逐级导航到目标功能区域（如"表单中心"→ 找到对应应用 → 打开 worksheet）
3. 导航过程中观察 URL hash 的变化，一旦出现所需的 worksheet ID 则停止

### 方法 B：修改 hash + 等待框架监听

有些 SPA 会监听 hashchange 事件，但 qiankun 子模块只在首次加载时匹配路由。尝试：
```javascript
window.location.hash = '#/application/{appId}/worksheet/{worksheetId}';
```
然后等待 3-5 秒看内容是否变化。

### 方法 C：直接 API 请求（需同源）

如果已知 appId 和 worksheetId，可以尝试直接请求后端 API：
```javascript
fetch('/api/sys-app/{appId}', {credentials: 'include'})
```
但注意：内部系统的 API 通常需要携带 Cookie 或 Authorization header。WebBridge 中 fetch 默认不携带 CORS credentials。

### 方法 D：截图确认 + 用户协助

当上述方法都失败时：
1. 截取当前页面截图保存
2. 告诉用户当前页面显示的内容
3. 让用户在自己的浏览器中打开该 URL，手动定位到 worksheet

## 已确认的 Fawkes 系统

| 系统 | URL | 框架 | 路由模式 |
|------|-----|------|----------|
| 华东院数字档案馆 | da.hdec.com | Fawkes Runtime + qiankun | `#/application/{appId}/worksheet/{worksheetId}` |

## 注意事项

- 默认加载模块不匹配不代表页面加载失败，而是 hash 路由未被框架正确解析
- Fawkes 框架的子模块通过 qiankun 加载，API 调用可能需要同源且携带特定 header
- 通过 `fetch` 从 evaluate 中发请求可能因不同源的 Cookie 策略返回"未登录"
- 深层链接需要用户在浏览器中手动打开后才会在 Fawkes 的 route history 中注册
