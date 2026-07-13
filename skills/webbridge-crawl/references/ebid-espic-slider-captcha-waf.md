# ebid.espic.com.cn 国家电投电商平台 — 滑块验证码 + 堡垒机 WAF

2026-07-12 技术分析报告。

## 站点基本信息

- **域名**: ebid.espic.com.cn（国家电力投资集团电子商务平台 / 电能e招采平台）
- **运营单位**: 电能易购（北京）科技有限公司
- **备案**: 京ICP备2020048793号 / 京公网安备 11010802038139号

## 页面架构

```
主页面 (bulletinListNew.html)
├── 顶部导航栏（首页、招标信息、采购信息、企业商城、个人商城、专家库等）
├── 左侧类目面板（招标公告/categoryId=2, 变更/二次公告/3, 中标候选人/5, 中标结果/4, 终止公告/6）
├── 右侧内容区: <iframe id="iframe" src="demo2.html?dates=300&categoryId=2&tenderMethod=01&tabName=招标公告&page=1">
└── 页脚（版权信息）
```

主页面是**纯导航外壳**。左菜单点击通过 JavaScript 切换 iframe 的 src：

```javascript
$("#bid").on("click","li a",function(){
    var cid = $(this).attr("id");
    var tabName = $(this).text();
    $("#iframe").attr('src',"//ebid.espic.com.cn/newgdtcms//category/demo2.html?dates=300&categoryId="+cid+"&tenderMethod=01&tabName="+tabName+"&page=1");
});
```

## iframe 真实列表页 (demo2.html)

当直接访问 `demo2.html` 时，页面会渲染一个**完整的滑块拼图验证码组件**（slidercaptcha），通过后才能看到公告列表。

### 滑块验证码 (slidercaptcha) 技术细节

#### 核心参数

| 参数 | 值 | 说明 |
|------|-----|------|
| width | 280px | canvas 宽度 |
| height | 155px | canvas 高度 |
| sliderL | 42 | 滑块边长 (px) |
| sliderR | 9 | 滑块圆角半径 (px) |
| offset | 5 | 容错偏移量 (px) |
| localImages | `Pic0.jpg` ~ `Pic4.jpg` | 5 张固定背景图 |

#### 图像资源

背景图 URL 模式：
```
//ebid.espic.com.cn//resource/gdtNew/images/Pic{0-4}.jpg
```

共 5 张图（随机选择），路径固定，不是动态生成的。

#### 验证码组件 DOM 结构

```
div#slideCode.slidercaptcha.card
├── div.card-header: "请完成安全验证"
└── div.card-body
    └── div#captcha (position:relative, 280px wide)
        ├── canvas (278x150) — 背景图（带缺口图片碎片）
        ├── i.refreshIcon.fa.fa-redo — 刷新按钮
        ├── canvas.block (63x150) — 可拖动的滑块拼图
        └── div.sliderContainer
            ├── div.sliderbg — 滑动轨道背景
            ├── div.sliderMask
            │   └── div.slider
            │       └── i.sliderIcon.fa.fa-arrow-right — 拖动箭头
            └── span.sliderText "向右滑动填充拼图"
```

#### 验证流程

1. 页面加载 → 通过 `localImages` 随机选择 Pic0-4.jpg → 绘制到 canvas
2. 在背景图上随机位置切割一个 42x42 的缺口（用贝塞尔曲线计算，sliderR=9）
3. 将切割下的拼图块绘制到 block canvas (63x150)，位置为缺口处的缩略图
4. 用户从 sliderContainer 的起点拖动 slider 到终点
5. 滑块拖动的位移映射到 block canvas 的 x 方向偏移
6. 松手后调用 verify() 将偏移量 `datas`（JSON 数组）POST 到服务器

```javascript
verify: function (arr, url) {
    var ret = false;
    $.ajax({
        url: url,
        data: { "datas": JSON.stringify(arr) },
        dataType: "json",
        type: "post",
        async: false,  // 同步 POST
        success: function (result) {
            ret = JSON.stringify(result);
        }
    });
    return ret;
}
```

`datas` 参数包含滑块拖动的**坐标轨迹数组**（含拖动过程中的多个采样点）。这是反爬的关键——自动化工具生成的轨迹通常是直线或标准曲线，而真人拖动的轨迹有自然抖动和速度变化。

#### 轨迹数据（trail）实际定义

```javascript
// 拖动过程中采集 Y 轴偏移量
var trail = [];
var handleDragMove = function (e) {
    var eventX = e.clientX || e.touches[0].clientX;
    var eventY = e.clientY || e.touches[0].clientY;
    var moveX = eventX - originX;
    var moveY = eventY - originY;
    if (moveX < 0 || moveX + 40 > that.options.width) return false;
    that.slider.style.left = (moveX - 1) + 'px';
    var blockLeft = (that.options.width - 40 - 20) / (that.options.width - 40) * moveX;
    that.block.style.left = blockLeft + 'px';
    that.sliderContainer.classList.add('sliderContainer_active');
    that.sliderMask.style.width = (moveX + 4) + 'px';
    trail.push(Math.round(moveY));  // 仅采集 Y 轴偏移，采样到整数
};
```

`trail` 只记录 Y 轴偏移量（每次 move 事件采样一次），最终传入 `verify()` 的 `arr` 就是这个数组。

#### 客户端双重校验逻辑

```javascript
_proto.verify = function () {
    var arr = this.trail; // Y轴偏移数组
    var left = parseInt(this.block.style.left);
    var verified = false;
    if (this.options.remoteUrl !== null) {
        verified = this.options.verify(arr, this.options.remoteUrl); // 服务端校验
    } else {
        // 客户端本地校验（未设 remoteUrl 时）：
        var average = arr.reduce(sum) / arr.length;
        var deviations = arr.map(function (x) { return x - average; });
        var stddev = Math.sqrt(deviations.map(square).reduce(sum) / arr.length);
        verified = stddev !== 0;  // ★ 只要轨迹不是一条直线就算通过
    }
    return {
        spliced: Math.abs(left - this.x) < this.options.offset,  // offset=5px
        verified: verified
    };
};
```

客户端校验只有两个条件：
1. **位置匹配**：滑块最终 X 偏移与缺口位置 `this.x` 的差值 < 5px
2. **轨迹非零方差**：Y 轴偏移数组的 `stddev !== 0`（即拖动过程中 Y 轴有自然抖动）

#### 验证通过后的跳转

滑块验证通过后，`onSuccess` 回调执行：
1. 设置 `Successfully` Cookie，**5 分钟有效**（`date.setTime(date.getTime()+5*60*1000)`）
2. 页面跳转到 `iframe.html?dates=300&categoryId=N&tenderMethod=01&tabName=...&page=1&time=当前日期`（格式 `2026/7/12`）

```javascript
onSuccess: function () {
    var date = new Date();
    date.setTime(date.getTime()+5*60*1000);
    $.cookie("Successfully", "Successfully", {expires: date, path: '/' });
    $.cookie("Successfully", "Successfully", {path: '/' });
    var localtime = new Date().getTime();
    var time = getData(localtime); // 格式: "2026/7/12"
    window.location = "//ebid.espic.com.cn/newgdtcms//category/iframe.html?dates=300&categoryId="+$('#categoryId').val()+"&tenderMethod="+$('#tenderMethod').val()+"&tabName="+$('#tabName').val()+"&page=1&time="+time;
}
```

`iframe.html` 才是验证通过后真正展示公告列表的页面。

#### 背景图拼合绘制

```javascript
var drawImg = function (ctx, operation) {
    var l = that.options.sliderL;  // 42
    var r = that.options.sliderR;  // 9
    var PI = that.options.PI;
    // 用 ctx.beginPath() + 贝塞尔曲线绘制拼图块形状
    // 在背景图上切割缺口
    // 用 ctx.drawImage(img, ...) 将拼图块绘制到 block canvas
}
```

#### 刷新机制

点击 refreshIcon（fa fa-redo）：
- 重新选择背景图（从 Pic0-4.jpg 随机）
- 重新计算缺口位置
- 重置滑块位置
- 不刷新页面，不改变其他 DOM 元素

### Tingyun（听云）前端监控

页面注入了 TingyunWeb RUM（Real User Monitoring）监控脚本：

```javascript
// 页面 HTML 中内联的干扰检测
var c = "__ty_web_inject_guard";
var o = "__ty_web_tpl_guard";

// 初始化
window.TingyunWeb("init", {
    "domain": "wkbrs2.tingyun.com",
    "token": "869c824fd7e846bb80e83fa9fc409ecd",
    "key": "eEgmer6hPu4",
    "id": "oSGCqaLZoDo",
    "requestTracing": {
        "propagators": ["tingyun"]
    }
});
```

性质：**被动监控**（性能采集 + 行为跟踪），非主动拦截。但配合服务器端的堡垒机 WAF 可识别非人类行为模式。

## 拦截层次

### 第一层：首页直接访问
访问 `bulletinListNew.html` → 页面正常渲染（导航栏、面包屑、左侧类目、iframe 占位），但 iframe 内不加载任何内容——因为 `demo2.html` 被滑块验证码拦住了。

### 第二层：iframe 列表访问
访问 `demo2.html` → 页面仅渲染滑块验证码组件，公告列表完全不可见。

### 第三层：验证后正常渲染
用户通过滑块验证后，浏览器获得 session token/Cookie，`demo2.html` 正常渲染公告列表表格（table 结构）。后续翻页通常不需要重复验证（会话保持）。

## 自动化爬取可行性评估（三维分析）

### 方法可行性汇总

| 方法 | 可行性 | 说明 |
|------|--------|------|
| curl/HTTP 直连 | ❌ 不可行 | 堡垒机 WAF 直接拦截 |
| Playwright headless | ❌ 不可行 | 滑块验证码无法通过 |
| WebBridge（自动） | ⚠️ 中等 | 见下方三维分析 |
| WebBridge + 用户手动（推荐） | ✅ 可行 | 用户手动拖动滑块，验证通过后提取数据 |
| HITL（用户粘贴数据） | ✅ 备用 | 用户手动在浏览器查看后粘贴公告列表 |

### 三维自动突破分析

#### 1. 缺口图像识别

背景图是 **5 张固定图片**（`Pic0.jpg` ~ `Pic4.jpg`），路径固定、非动态生成。

- **可行性：高**
- 方法：下载 5 张原图，用 OpenCV 模板匹配（`cv2.matchTemplate`）检测缺口位置
- 已知拼图块大小 42x42，所以缺口 X 坐标容易定位
- 对比动态验证码平台（如极验、网易易盾）每请求渲染新背景图，此处固定图片极大降低了图像识别门槛

#### 2. 轨迹模拟

客户端校验极弱——只检查 Y 轴偏移的 `stddev !== 0`，即拖拽过程中 Y 方向有自然抖动即可。任意带随机噪声的数组如 `[0, 1, -1, 2, 0, -1, 1]` 都能通过客户端的 stddev 检查。

**关键未知量**：服务端校验逻辑（`remoteUrl` 模式下的 `this.options.verify(arr, url)`）。服务器会收到 `datas=JSON.stringify(arr)`（Y 轴偏移数组），可能检查：
- 采样点数量是否合理（真人拖拽通常 15-50 个采样点）
- X 轴移动的连续性和速度曲线（真人加速-减速曲线 vs 线性移动）
- 总拖拽时长（过快 <200ms 或过慢 >5s 均异常）
- Y 轴抖动模式（特定频率抖动 vs 自然随机抖动）
- 与 Tingyun 采集的行为事件是否一致

- **客户端侧可行性：高**（stddev !== 0 易绕过）
- **服务端侧可行性：中等**（需反向工程或试错）

#### 3. Cookie 时效与会话保持

| 约束 | 影响 |
|------|------|
| `Successfully` cookie 有效期 | **5 分钟**（代码 `date.setTime(date.getTime()+5*60*1000)`） |
| 验证后跳转到 `iframe.html` | 非 `demo2.html` 原地展示，需要处理重定向 |
| 类目切换是否需要重新验证 | 未知——如有 session 级 cookie，则验证一次全局有效；如每个 iframe 实例独立验证，则需重复 5 次 |
| 翻页是否需要重新验证 | 未知——需实际测试 |

#### 综合判断

```
自动突破可行性：50-60%（中等偏低）
瓶颈：服务端轨迹校验逻辑未知 + 5分钟 cookie 窗口短
最佳实践：WebBridge 打开后用户手动拖一次，后续 AI 工具自动提取
```

## 与阿里云 WAF 的区别

| 特征 | 国家电投（ebid.espic.com.cn） | 阿里云 WAF（acw_sc__v2） |
|------|-------------------------------|--------------------------|
| 验证类型 | 滑块拼图（需要用户交互） | JS Cookie 挑战（自动完成） |
| 是否可自动化 | 否（需要真人拖拽） | 是（headless 浏览器可过） |
| 首页访问 | 正常渲染（无内容） | 直接挑战 |
| iframe/子路径 | 滑块验证码独立渲染 | 统一 Cookie 挑战 |
| 会话保持 | 验证一次后保持 | Cookie 过期后重新挑战 |
| 动态资源 | 使用固定图片（Pic0-4.jpg） | 动态生成验证内容 |

## 观察到的行为模式

1. 直接访问 `bulletinListNew.html`：导航和类目均正常渲染（都是纯 HTML + CSS），**不是 WAF 拦截页**，而是正常的导航外壳——真正的 WAF 在 iframe 内的 `demo2.html`
2. 用户点击左侧类目切换时，JS 更改 iframe 的 src 指向不同的 `demo2.html?categoryId=N...`
3. 每个 iframe 加载都需要**独立的滑块验证**（因为 demo2.html 每个 instance 都独立渲染验证码）
4. 可能验证一次后全局 session 通过，后续 iframe 切换不需要重复验证（需要实际测试确认）
5. 网站有移动端自适应（`touch-action: none` 禁止 touch 事件穿透）
6. 直接 curl/HTTP 访问 `bulletinListNew.html` 会看到「WEB 应用防火墙」纯文本拦截页（`web_extract` 提示），但真实浏览器访问时返回的是正常 HTML 页面（完整导航、左侧类目），只有 iframe 内 `demo2.html` 被滑块验证码挡住
