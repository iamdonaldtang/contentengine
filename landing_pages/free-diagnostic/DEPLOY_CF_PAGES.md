# 部署指南 · Cloudflare Pages + Workers Route

> **目标**：把 `engine/landing_pages/free-diagnostic/` 部署到 Cloudflare Pages，通过 Workers Route 让用户访问 `taskon.xyz/free-diagnostic` 时实际返回 Pages 的内容。
>
> **总耗时**：~1 小时（DevOps / 你自己都能跑）
>
> **前置**：`taskon.xyz` DNS 已托管在 Cloudflare（Donald 已确认）
>
> **关联文档**：
> - 上层 PRD：`D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\PRD_前后端集成需求_W1精简版.md`
> - 子包总说明：`engine/landing_pages/README.md`

---

## 0 · 部署架构图

```
[用户浏览器]
  ↓ 访问 https://taskon.xyz/free-diagnostic
[Cloudflare Edge]
  ↓ DNS 命中 taskon.xyz（CF 托管）
[CF Workers Route]
  ↓ 匹配 taskon.xyz/free-diagnostic/*
[CF Pages 项目 · taskon-landing.pages.dev]
  ↓ 返回 index.html + styles.css + JS
[用户浏览器渲染]
  ↓ JS 触发 POST
[https://ingest.taskon.xyz]
  ↓ CF Tunnel
[engine 容器 :5051 ingestion]
  ↓ 写
[engine SQLite]
```

---

## 1 · 前置阻塞清单（**部署前必须解决**）

### ❌ 阻塞 1 · engine ingestion 必须改字段处理

当前 ingestion `/api/landing-signup` 只接受 `email`。本次 V1 加了 2 个字段 `telegram_handle` + `project_url`，必须改后端：

```python
# engine/ingestion/landing_signup.py (or similar)
# 当前 schema 只认 email · 加这两个字段:
class LandingSignupBody(BaseModel):
    email: EmailStr | None = None
    cookie_id: str
    page_path: str
    url: str
    referrer: str | None = None
    impression_only: bool = False
    form_data: dict | None = None  # ★ V1 加 · 含 telegram_handle / project_url
    timestamp: str | None = None

# 处理逻辑里把 form_data 写到 leads 表的 metadata 列
# 或者新增 leads.telegram_handle / leads.project_url 列
```

工时：engine 维护者 30min。

### ❌ 阻塞 2 · engine ingestion 必须配 CORS

```python
# 加 middleware
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://taskon.xyz"],  # ★ 不要用 * 通配
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
    max_age=3600,
)
```

工时：engine 维护者 10min。

### ❌ 阻塞 3 · ingest.taskon.xyz CF Tunnel

DevOps 配 CF Tunnel：

```bash
# 在 engine 服务器（或 Donald 桌面）
cloudflared tunnel login
cloudflared tunnel create taskon-ingest
# 配置 ~/.cloudflared/config.yml:
tunnel: <tunnel-id>
credentials-file: /home/user/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: ingest.taskon.xyz
    service: http://localhost:5051
  - service: http_status:404

cloudflared tunnel route dns taskon-ingest ingest.taskon.xyz
cloudflared tunnel run taskon-ingest

# 持久化为 systemd 服务（生产环境）
sudo cloudflared service install
```

工时：DevOps 30min。

### ✅ 已就绪

- HTML + CSS（本子包）
- 3 个 JS（engine/frontend_snippets/）
- engine 容器 ingestion 框架（仅缺 form_data 字段 + CORS）
- Lark Webhook（lead 进时自动通知 BD）
- engine SQLite + attribution_engine

---

## 2 · 部署 5 步（解决阻塞后跑）

### Step 1 · 把代码推到 GitHub

如果 `engine` 仓还没在 GitHub 上：

```bash
cd D:\Taskon\marketing\engine
git init
git add landing_pages/
git commit -m "feat: add /free-diagnostic landing page V1"
gh repo create taskon-landing --private --source=. --push
```

或者**只推 landing_pages 子目录**（如果 engine 主仓不想公开）：

```bash
# 创建独立 repo 只放落地页
mkdir D:\Taskon\marketing\taskon-landing
cd D:\Taskon\marketing\taskon-landing

# 复制需要的文件
xcopy /E ..\engine\landing_pages\* .
xcopy /E ..\engine\frontend_snippets\*.js js\
copy ..\engine\landing_pages\shared\taskon-base.css css\

# 整理目录结构
# free-diagnostic/
# ├── index.html
# ├── styles.css
# ├── assets/
# css/taskon-base.css
# js/taskon_uid.js / landing_impression.js / landing_form_submit.js

git init
git add .
git commit -m "Initial commit · /free-diagnostic V1"
gh repo create taskon-landing --private --source=. --push
```

**建议**：用第二种方式（独立 repo），CF Pages 拉 repo 更干净。

### Step 2 · 在 Cloudflare 后台创建 Pages 项目

1. 登录 [dash.cloudflare.com](https://dash.cloudflare.com)
2. 左侧 → **Workers & Pages** → **Create application** → **Pages** → **Connect to Git**
3. 授权 GitHub → 选 `taskon-landing` repo
4. **Build settings**：
   - Production branch: `main`
   - Build command: 留空（纯静态）
   - Build output directory: `/`（如果 repo 根直接是 index.html 等）
5. **Save and Deploy**
6. 等 ~30s 部署完成 → 拿到 `<project-name>.pages.dev` 域名

### Step 3 · 测试 Pages 部署

打开 `<project-name>.pages.dev/free-diagnostic/index.html`（取决于你的 repo 结构）：

- ✅ 页面正常渲染
- ✅ 表单可以填写
- ⚠️ 提交会 fail（因为 `ingest.taskon.xyz` 还没好，或 CORS 没配，先忽略）

### Step 4 · 配 Workers Route 让 taskon.xyz/free-diagnostic 路由到 Pages

在 CF 后台：

1. **Workers & Pages** → **Create application** → **Create Worker**
2. 命名 `taskon-landing-router`，粘贴下面代码：

```javascript
// taskon-landing-router worker
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 落地页 origin（Step 2 拿到的）
    const PAGES_ORIGIN = 'https://taskon-landing.pages.dev';

    // 仅处理 /free-diagnostic 及其子路径
    if (!url.pathname.startsWith('/free-diagnostic')) {
      return fetch(request);  // 其他路径透传
    }

    // 改写到 Pages
    const newUrl = new URL(url.pathname + url.search, PAGES_ORIGIN);

    // 复制请求 + 改 host
    const newRequest = new Request(newUrl.toString(), {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: 'follow',
    });

    const response = await fetch(newRequest);

    // 复制响应（保留所有 headers / cache 等）
    const newResponse = new Response(response.body, response);

    // 强制 HTML cache-control 短（让你修改后秒级生效）
    if (response.headers.get('content-type')?.includes('text/html')) {
      newResponse.headers.set('Cache-Control', 'public, max-age=60');
    }

    return newResponse;
  },
};
```

3. **Save and Deploy**
4. 进入这个 Worker → **Settings** → **Triggers** → **Add route**
5. Route: `taskon.xyz/free-diagnostic*`
6. Zone: `taskon.xyz`
7. **Save**

### Step 5 · 测试 taskon.xyz/free-diagnostic

```
打开浏览器访问: https://taskon.xyz/free-diagnostic
期望: 看到落地页内容（与 Pages 域名访问相同）

检查项:
  ✅ URL 是 taskon.xyz/free-diagnostic（不是 .pages.dev）
  ✅ HTTPS 锁正常
  ✅ DevTools Console 无 CSP 错误
  ✅ DevTools Network impression POST 到 ingest.taskon.xyz（需 §1 阻塞解决后）
  ✅ 填表单 → POST signup 201 + lead_id
```

---

## 3 · 验收 Checklist（共 12 条）

复制下面到飞书 / Lark 给运维 + 后端逐条勾选：

```
基础（部署后立即可验）:
  [ ] https://taskon.xyz/free-diagnostic 200 OK
  [ ] DevTools Console 无 error
  [ ] 桌面 Chrome / Safari / Firefox / Edge 正常
  [ ] iOS Safari / Android Chrome 正常
  [ ] LCP < 2.5s (PageSpeed Insights)
  [ ] CSP header 正确（Network → Response Headers）

UTM + 埋点（需后端 ingestion ready 后）:
  [ ] 带 UTM 5 段访问 → 表单 hidden field 填充
      测试 URL: https://taskon.xyz/free-diagnostic?utm_source=twitter&utm_medium=thread&utm_campaign=test&utm_content=test&utm_term=test
  [ ] onload 触发 POST /api/landing-signup impression 201
  [ ] Cookie _taskon_uid 写入 (Application → Cookies)
  [ ] 填表单 3 字段 → POST signup 201 + lead_id
  [ ] success state 显示（form 隐藏）
  [ ] Lark 群收到 BD 通知（含 email + tg + project_url + 5 段 UTM）

后端落库验证:
  [ ] engine SQLite user_journey 有 impression + signup 各 1 行
  [ ] engine SQLite leads 有 1 行 + first_utm_campaign 命中
```

---

## 4 · 常见问题

### Q1 · `taskon.xyz/free-diagnostic` 访问 404？

检查：
1. CF Workers Route 是否正确配 `taskon.xyz/free-diagnostic*`（注意 `*` 通配尾部）
2. Worker 是否 Save and Deploy 了
3. 浏览器清缓存（CF 可能 cache 旧的 404）

### Q2 · 表单提交 CORS error？

检查：
1. engine ingestion `Access-Control-Allow-Origin` 是否 `https://taskon.xyz`
2. **preflight OPTIONS** 是否也返回 CORS headers（FastAPI middleware 自动处理）
3. `ingest.taskon.xyz` CF Tunnel 是否在线

测试：

```bash
curl -X OPTIONS https://ingest.taskon.xyz/api/landing-signup \
  -H "Origin: https://taskon.xyz" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  -i
# 期望: 200 + Access-Control-Allow-Origin: https://taskon.xyz
```

### Q3 · `_taskon_uid` cookie 写不进去？

检查：
1. 用 HTTPS 访问（cookie `Secure` flag 要求）
2. 用户没拒绝 Cookie Consent
3. 浏览器隐私模式（cookie 拒写是预期）

### Q4 · CF Pages 部署后 og:image 404？

og-image.png 是后期补的占位资产。临时 fallback：在 HTML 里把 `<meta property="og:image">` 改成 taskon 主站默认 og 图（如果有）。或者忽略，社交分享时无缩略图。

### Q5 · 怎么改文案 / 表单字段？

```
1. git pull engine repo
2. 改 engine/landing_pages/free-diagnostic/index.html
3. git commit + push
4. CF Pages 自动重新部署（CI/CD）
5. ~30s 后 taskon.xyz/free-diagnostic 显示新内容
```

**不需要排前端，不需要打扰运维**。

### Q6 · 怎么回滚？

CF Pages 后台 → Project → Deployments → 找上一个 working version → **Rollback**。秒级回滚。

---

## 5 · 后续运维事项

### 5.1 · 监控

- CF Pages 后台看 deployments 状态
- CF Workers 后台看 Worker 调用量（免费 tier 每天 100K 次，够用）
- engine 容器 :5051 `/health` endpoint 监控
- Lark 接 P1 告警（engine 已实现）

### 5.2 · 缓存

CF Pages 默认 cache 静态资源。HTML 已在 Worker 里设 `max-age=60`（1min），改了文案后 1min 内生效。

### 5.3 · SSL

完全免费 + 自动续。CF 自动管理。

### 5.4 · DDoS 防护

CF 标配。免费 tier 够 V1 流量。

### 5.5 · 自定义域名直绑（可选优化）

如果未来 `/free-diagnostic` 流量大，可以把 Pages 项目**直接绑定**到 `landing.taskon.xyz` 子域（CF Pages → Custom Domains），用主站 301 redirect `/free-diagnostic → landing.taskon.xyz`。但这是 V2 优化，V1 不需要。

---

## 6 · 升级路径 · V2 GrowthScan 完整版

V2 时：
1. 把 `growthscan/frontend/` 推到同一个 CF Pages（或新建项目）
2. CF Workers Route 改：`taskon.xyz/free-diagnostic*` → 新 Pages
3. URL 不变 → 历史 UTM 短链全部继续可用

---

## 7 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-15 | 首版 · CF Pages + Workers Route 部署方案 · DNS 已确认在 CF |
