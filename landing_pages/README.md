# engine/landing_pages · 内容营销引擎 · 落地页子包

> **本子包定位**：TaskOn 内容营销引擎所有「外站落地页」的源代码集合，与 `frontend_snippets/`（落地页埋点 JS）+ `ingestion/`（POST endpoint）三位一体构成"用户从内容引擎引流 → 留资 → 归因"的完整前端层。
>
> **维护方**：engine 维护者（Donald + Claude）
> **部署位置**：**taskon 服务器**（不是 engine docker 容器）
> **数据回流**：落地页 → `ingest.taskon.xyz`（CF Tunnel）→ engine 容器 :5051 → SQLite
>
> **关联**：
> - `D:\Taskon\marketing\00_内容营销引擎\全流程规划_v3\PRD_前后端集成需求_W1精简版.md` Section C（架构决策）
> - `D:\Taskon\marketing\engine\frontend_snippets\README.md`（3 JS 工程实施细则）
> - `D:\Taskon\marketing\engine\docs\功能清单与外部访问.md` §2（ingestion endpoints）

---

## 1 · 目录结构

```
engine/landing_pages/
├── README.md                          ← 本文件
│
├── free-diagnostic/                   ★ W1 P0 实施 · taskon.xyz/free-diagnostic
│   ├── index.html                     ← 完整 HTML
│   ├── styles.css                     ← 页面特定样式
│   └── assets/                        ← 图片 / OG / favicon（待 Donald 提供）
│
├── shared/                            跨落地页共享资源
│   ├── taskon-base.css                ← 基础样式（CSS variables / reset / typography）
│   ├── nav-partial.html               ← TaskOn nav HTML（V2 用 · V1 直接 inline 进 index.html）
│   ├── footer-partial.html            ← 同上
│   └── js/
│       └── README.md                  ← 说明：JS master 在 ../../frontend_snippets/
│
├── growth-playbook/                   (V2 · M2) · 暂未实施
│   └── (待 V2 阶段创建)
│
└── benchmark-report/                  (V3 · 待 Donald 数据脏立场最终决策)
    └── (暂未实施)
```

---

## 2 · 当前 W1 状态

| 落地页 | 路径 | 状态 |
|---|---|---|
| free-diagnostic | `taskon.xyz/free-diagnostic` | ✅ V1 简化版已写 · 3 字段表单（Email + Telegram + Project URL） |
| growth-playbook | `taskon.xyz/growth-playbook` | ⏳ V2 后续（M2） |
| benchmark-report | `taskon.xyz/benchmark-report` | ⏳ V3 后续（Donald 决策） |

V1 简化目标：用户填 3 字段 → engine 写 leads + Lark 通知 BD → BD 24h 内人工产出诊断报告。

V2 升级目标：替换为 GrowthScan 完整 SPA（输入项目名 → 30s 生成 6 维度雷达图报告）。URL 不变 → 历史 UTM 短链全部继续可用。

---

## 3 · 给 taskon 运维的部署摘要（10 分钟）

> **完整部署手册**：本目录暂未提供 deploy/ 子目录（由 taskon 运维 + DevOps 协作产出 nginx.conf + DEPLOY.md）。下面是关键约束。

### 3.1 · 静态资源部署

把 `free-diagnostic/` + `shared/` + `../frontend_snippets/*.js` 复制到 taskon 静态资源服务器：

```
/var/www/taskon.xyz/
├── free-diagnostic/
│   ├── index.html         (来自 engine/landing_pages/free-diagnostic/)
│   ├── styles.css
│   └── assets/
├── css/
│   └── taskon-base.css    (来自 engine/landing_pages/shared/)
└── js/
    ├── taskon_uid.js              (来自 engine/frontend_snippets/)
    ├── landing_impression.js      (来自 engine/frontend_snippets/)
    └── landing_form_submit.js     (来自 engine/frontend_snippets/)
```

### 3.2 · nginx 路由

```nginx
server {
    server_name taskon.xyz;

    # 落地页静态资源
    location /free-diagnostic {
        alias /var/www/taskon.xyz/free-diagnostic;
        try_files $uri $uri/ /free-diagnostic/index.html;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; connect-src 'self' https://ingest.taskon.xyz; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; font-src 'self' data:;" always;
    }

    # 共享 CSS / JS
    location /css/  { alias /var/www/taskon.xyz/css/; }
    location /js/   { alias /var/www/taskon.xyz/js/; }
}
```

### 3.3 · DevOps CF Tunnel

需要 DevOps 配置：

```
ingest.taskon.xyz → CF Tunnel → engine 容器 :5051
```

落地页 JS POST 走 `https://ingest.taskon.xyz/api/landing-signup` → engine ingestion 写 SQLite。

### 3.4 · engine ingestion 侧配置

**必须**配置 `Access-Control-Allow-Origin: https://taskon.xyz`（CORS），否则浏览器拦截 POST。

---

## 4 · 升级路径（V1 → V2 → V3）

| 阶段 | 时间 | 落地页内容 | URL | 后端 |
|---|---|---|---|---|
| **V1** | W1（now） | 3 字段表单 + Lark 通知 + BD 人工产出 | `/free-diagnostic` | engine ingestion |
| V1.5 | M2 | 加 scroll milestone + cta_click 埋点 | URL 不变 | engine 加 endpoint |
| V2 | M3 | 替换为 GrowthScan 完整 SPA · 输入项目名 → 30s 报告 | URL 不变 ★ | growthscan FastAPI |
| V2.5 | M4 | 加 SEO benchmark `/benchmarks/perps-dex` 等 | 新增 URL | growthscan |
| V3 | M6 | A/B 测试框架 + Cohort 分析 + Markov 归因 | URL 不变 | engine + growthscan |

**关键纪律**：`/free-diagnostic` URL 永远不变 → 历史 UTM 短链全部继续可用。

---

## 5 · 本子包维护规则

1. **shared/js/ 目录不能放 JS 文件副本**——master 永远在 `../frontend_snippets/`。打包脚本（你 / 运维）负责 copy 到部署目标。
2. **HTML 改文案 / 表单字段 → 走 engine 仓 git commit → 给运维更新 → 不需要排前端**
3. **HTML 改样式（CSS framework / design system）→ 与 taskon 前端协调（保持一致性）**
4. **新增落地页**：在 `engine/landing_pages/` 下加新目录（如 `growth-playbook/`），更新本 README §1 目录结构 + §2 当前状态表
5. **V2 升级**：本 V1 文档归档到 `archive/`，新出 V2 文档

---

## 6 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-15 | 首版 W1 子包创建 · free-diagnostic V1 简化版 |
