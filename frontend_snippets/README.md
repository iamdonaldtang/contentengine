# TaskOn Landing-Page Tracking Snippets

> **谁该读这个**:负责 `taskon.xyz/benchmark-report`、`/free-diagnostic`、`/growth-playbook` 三个落地页的前端工程师。
> **产物**:把 3 个 JS 文件嵌入落地页 + 配 1 个 env 变量。
> **效果**:落地页浏览 + 表单提交都能被引擎归因到具体的 X / LinkedIn / Newsletter 等内容来源(B1 §4.3)。

---

## 0 · 全局背景:为什么前端要埋这两条

TaskOn 内容营销引擎跑在内部 SQLite(`engine/runtime/state.db`),归因模型需要把"用户在 Twitter 看到 thread → 点击进落地页 → 一周后留资"3 个事件串成同一条 `user_journey`。

前端发两条 POST:

| 触点 | 路由 | 频率 | 触发 |
|---|---|---|---|
| **浏览** | `POST /api/landing-signup` body 含 `impression_only:true` | 每次落地页 load(同 tab 内去重) | onload |
| **留资** | `POST /api/landing-signup` body 含 `email` | 表单提交 | submit |

两条都带 `cookie_id`(30 天 first-party cookie)。引擎周日 21:00 跑归因时,用 `cookie_email_map` 把同 cookie 的浏览行 ∪ 同 email 的留资行 = 完整 user_journey。

---

## 1 · 嵌入步骤(3 个落地页都做)

### 1.1 · 把 3 个 JS 文件部署到落地页静态资源目录

```
/public/js/taskon_uid.js
/public/js/landing_form_submit.js
/public/js/landing_impression.js
```

文件原版在 engine 仓 `D:\Taskon\marketing\engine\frontend_snippets\` 下,**直接复制,不要改动**(改动请提 PR 到 engine 仓)。

### 1.2 · 在每个落地页底部 `</body>` 之前嵌入

```html
<script>
  // 注意:这个 BASE 必须先配,在 script 加载前。
  window.TASKON_INGEST_BASE = 'https://ingest.taskon.xyz';
  // 可选:自定义表单选择器(默认 [data-taskon-signup] 或第一个 <form>)
  window.TASKON_FORM_SELECTOR = '[data-taskon-signup]';
  // 可选:留资成功跳转
  window.TASKON_THANK_YOU_URL = '/thank-you';
</script>
<script src="/js/taskon_uid.js"></script>
<script src="/js/landing_impression.js"></script>
<script src="/js/landing_form_submit.js"></script>
```

**加载顺序必须**:`taskon_uid` 第一,后两个谁先谁后无所谓。

### 1.3 · 落地页表单 HTML 标记

```html
<form data-taskon-signup>
  <input type="email" name="email" required placeholder="your@email.com" />
  <button type="submit">免费下载 Q1 Benchmark</button>
  <!-- 可选错误 / 成功 slot -->
  <p data-taskon-error style="display:none;color:#c00"></p>
  <p data-taskon-success style="display:none;color:#080">已收到。我们 24h 内联系你。</p>
</form>
```

`data-taskon-signup` 属性帮 JS 选中你的表单。若没有,JS 会选页面里第一个 `<form>` 兜底。

---

## 2 · UTM 纪律(链接进入落地页时必须满 5 段)

引擎的 `lib.utm.parse_utm` 要求 5 段都齐才能归因:

```
https://taskon.xyz/benchmark-report
  ?utm_source=twitter
  &utm_medium=thread
  &utm_campaign=2026w19_thread01
  &utm_content=donald_en
  &utm_term=47pct_bot
```

**少任意一段 → 整条 UTM 当 None 入库**,这条流量来源就只能归到 "(direct)"。请前端在「分享链接」「按钮跳转」「邮件 CTA」类组件里都校验 5 段。

如果你的 CMS 不允许 utm_term 字段,告诉 engine 维护者(Donald),我们把 fallback 改成 4 段也行。

---

## 3 · CSP / Cookie 注意事项

### 3.1 · Content-Security-Policy

需要把 ingest 域加进 `connect-src`:

```
Content-Security-Policy: connect-src 'self' https://ingest.taskon.xyz;
```

### 3.2 · Cookie

`_taskon_uid`(30 天,第一方 cookie) 由 `taskon_uid.js` 写入:
- `SameSite=Lax`
- `Secure`(只 HTTPS 写,HTTP 静默失败 — fail-closed)
- `Path=/`

无第三方 cookie 依赖。

### 3.3 · GDPR / Cookie Consent

如果落地页挂了 Cookie Consent 横幅:
- `_taskon_uid` 属于「分析/统计」类别(non-essential)
- 用户拒绝时,前端应该 **不加载** `landing_impression.js` 和 `landing_form_submit.js`,但 **可以**让 `taskon_uid.js` 等用户接受后再加载

---

## 4 · 快速验证(部署完跑一次)

### 4.1 · DevTools Network 面板

打开 `/benchmark-report` → Network → 应看到 1 个 POST `/api/landing-signup`,Request body:

```json
{
  "impression_only": true,
  "cookie_id": "<UUID>",
  "page_path": "/benchmark-report",
  "url": "https://taskon.xyz/benchmark-report?utm_source=...",
  "referrer": "https://t.co/..."
}
```

返回 201 + `{"mode":"impression","status":"ok"}`。

### 4.2 · 表单提交

填邮箱提交 → 应看到 1 个 POST 同 URL,body 含 `email`,**不**含 `impression_only`。返回 201/200 + `{"lead_id":N,"is_new":true|false,"status":"ok"}`。

### 4.3 · 引擎侧确认

让 engine 维护者跑(只读):

```bash
docker compose exec engine python -c "from lib.db import db; \
  [print(dict(r)) for r in db.fetchall(\"SELECT user_id,action,utm_campaign,page_path FROM user_journey ORDER BY id DESC LIMIT 5\")]"
```

应看到刚才浏览 + 留资的 2 行。

---

## 5 · 截图位

部署完请把截图发给 engine 维护者补到这里:

- [ ] DevTools Network 面板 impression POST 截图
- [ ] DevTools Network 面板 signup POST 截图
- [ ] Cookie 写入截图(Application → Cookies → taskon.xyz)
- [ ] 三个落地页都嵌入完成的部署截图

---

## 6 · 反向兼容 / 失败模式

| 场景 | 行为 |
|---|---|
| `TASKON_INGEST_BASE` 未配 | console.warn,impression 跳过;表单显示"配置缺失" |
| `crypto.randomUUID` 不存在 | 退化 `Math.random` v4 UUID(antique 浏览器) |
| `navigator.sendBeacon` 不存在 | 退化 `fetch(keepalive: true)` |
| `sessionStorage` 被禁 | impression 每次 load 都发(没法去重,后端可处理) |
| 用户阻止 cookie | cookie_id 为空 → engine 拒绝 impression(400) |
| 网络挂 | 表单提交显示"网络错误,请稍后重试";impression 静默失败 |

---

## 7 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-05-13 | 首版 · T8-T11 实施 |
