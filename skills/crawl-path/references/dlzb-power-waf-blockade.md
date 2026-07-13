# dlzb_power 阿里云 WAF 阻断记录（2026-07-03）

## 问题

dlzb_power (电力招标网, https://www.dlzb.com/zb/search.php?kw=BIM) 定时同步持续失败

## 根因

www.dlzb.com 的 `/zb/` 路径部署了 **阿里云 WAF**（Web Application Firewall）。

## WAF 识别特征

| 特征 | 说明 |
|------|------|
| HTTP 响应 | 200 OK（不是 403），但返回的是 WAF 验证页面而非真实内容 |
| 页面脚本 | 大量混淆 JS（eval + Cookie 设置 + Math.random 反爬验证） |
| 隐藏字段 | 隐藏 `<textarea id="renderData">` 存有 WAF 挑战 JSON（traceid 等） |
| meta 标签 | `<meta name="aliyun_waf_aa">` 和 `<meta name="aliyun_waf_bb">` |
| Cookie | 设置了 `acw_tc`（阿里云 WAF Cookie） |
| 显示内容 | 页面只显示 WAF 验证 `<script>`，不渲染任何内容 |

## 测试方法

```bash
# 检查是否被 WAF 拦截
curl -sL --connect-timeout 10 'https://www.dlzb.com/zb/search.php?kw=BIM' 2>&1 | grep -c "renderData\|aliyun_waf"
# >0 说明被 WAF 拦截
```

## 关键发现：子域名不受影响

| 域名 | WAF 状态 | 站点用途 |
|------|---------|----------|
| `www.dlzb.com/zb/` | **被 WAF 保护** | 搜索/聚合页 |
| `zgjtjs.dlzb.com` | 正常 | 中国交通建设集团 |
| `zhfdc.dlzb.com` | 正常 | 鲁班商务网 |
| `tjbid.dlzb.com` | 正常 | 中国铁建物资采购网 |

所有子域名子站（*.dlzb.com）的列表页和搜索结果页**不受 WAF 防护**，可正常用 curl 获取 HTML。

## 当前处理

- `dlzb_power` 仍保持 `enabled: true` 但无法成功
- 如需恢复，需要以下方案之一：
  1. WAF IP 白名单：将爬虫服务器 IP 提交给 www.dlzb.com 站点管理员
  2. 代理绕过：配置 HTTP_PROXY 通过对 www.dlzb.com 有白名单的代理转发请求
  3. VPN 方案：在能正常访问 www.dlzb.com 的内网机器上部署爬虫
  4. 或者放弃该站点，因为子域名子站已经覆盖了中铁建、中交建、鲁班等各集团的公告

## 注意

此问题无法通过修改 crawl_rules 绕过 -- WAF 在 http 层面拦截的是请求 IP / UA / 指纹，与页面选择器/crawl 策略无关。浏览器（Playwright 或 WebBridge）加载页面时也返回 WAF 页面，因为 WAF 对初次访问者的浏览器 JS 执行有要求。

**不要将 WAF #renderData 与 Destoon CMS 搜索页 #renderData 混淆**：
- WAF：`#renderData` 内容是 JSON 挑战数据（`{"traceid":"...", "l1":"...", "l2":"..."}`）
- Destoon：`#renderData` 内容是 CMS 的空字段数据
- 区分方法：curl 能否获取真实内容
