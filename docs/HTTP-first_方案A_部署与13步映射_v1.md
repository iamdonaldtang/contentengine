# HTTP-first（方案A）· 部署 + 13 步 → 端点映射 · v1（2026-06-03）

> **为什么有这份文档**：全流程操作手册 v3 假设 Cowork 能 SMB 写 `Z:\drafts`、能 Tailscale `ssh donald@engine`、能跑 `run_remote.ps1`。**实测这三样在 Cowork sandbox 里全部不可达**（网络盘写入被拒、`engine:5051` HTTP 000、无 PowerShell）。Cowork 唯一能用的是 ① 本地挂载盘 ② 公网 HTTPS。本方案把 13 步全部改走公网 `ingest.taskon.xyz` 的 admin 端点，Cowork 只 `curl`，端到端即时、无人工搬运、永不碰 Z:/SSH。

---

## 1 · 新增了什么

**引擎端**（`ingestion/admin_routes.py`，复用已有 Bearer 鉴权 + `_spawn_task` 异步底座）：

| 端点 | 作用 | 替代手册里的 |
|---|---|---|
| `POST /admin/runtime-file/<name>` | 写 runtime 根 json（白名单 `hot_topics_*` / `candidates_pool_*`） | Z: 写 hot_topics |
| `POST /admin/drafts/<piece>/<file>` | 写文本草稿 `.md/.yaml/.yml/.json/.txt`（建目录、防穿越） | SMB 写稿 |
| `GET /admin/drafts/<piece>/<file>` | 读文本草稿 | SMB 读稿 |
| `GET /admin/drafts/<piece>` | 列 piece 文件 + DB state | 审稿前看清单 |
| `POST /admin/jobs/<job>` | 白名单 job 异步派发（容器内 `python -m jobs.*`） | `run_remote.ps1 -Job` |
| `POST /admin/pieces/<piece>/state` | 安全状态迁移（枚举校验 + 参数化 update_state） | `-Sqlite UPDATE` |
| `POST /admin/pieces/<piece>/select` | 校验 selection_card.yaml → 置 selected | `validate_selection` |
| `POST /admin/pieces/<piece>/kill` | 数据关失败=砍：FK 级联删子表 + 删行 + 删目录 | `-Sqlite DELETE` + 删目录 |

已存在、本方案直接复用：`GET /health`、`/admin/health/all`、`POST /admin/run_publish`（急发）、`GET /admin/tasks/<id>`（轮询）。

job 白名单：`adapter_orchestrator · voice_checker · custom_slice_generator · mpt_runner · utm_generator · schedule_planner · kol_relation_tracker`（每个有逐参数校验）。

**Cowork 端**：`scripts/tk.sh` —— 一条 source 进去，13 步每步一个函数。

---

## 2 · 部署（你在引擎机做一次）

```powershell
# 引擎机（Tailscale host engine）
cd D:\engine-host\taskon\engine

# 1 · 确认 ADMIN_API_TOKEN 已设（空=admin 全局禁用）。没有就生成一个强 token：
#     在 .env 里设 ADMIN_API_TOKEN=<32+位随机串>
#     已设则跳过；要轮换就改这里。
notepad .env        # 检查/设置 ADMIN_API_TOKEN

# 2 · 重建 ingestion（admin_routes.py 在 ingestion 镜像里）
docker compose up -d --build ingestion    # 或 engine，看 compose 服务名

# 3 · 冒烟
curl https://ingest.taskon.xyz/health
curl -H "Authorization: Bearer <token>" https://ingest.taskon.xyz/admin/health/all
```

> 代码已在 Cowork 侧用临时 sqlite + Flask test client 跑了 34 条断言全绿（鉴权、路径穿越、白名单、state 枚举、select、kill 的 FK 级联）。但**引擎机真容器里仍需 step 2-3 冒烟**确认 .env token 生效。

---

## 3 · 13 步 → tk.sh / 端点 映射（Cowork 侧）

```bash
# 一次性
export ADMIN_API_TOKEN='<引擎机 .env 的 ADMIN_API_TOKEN>'
source <挂载路径>/Taskon/marketing/engine/scripts/tk.sh
```

| 步 | 动作 | tk.sh | 底层端点 |
|---|---|---|---|
| 0 | 入场检查 | `tk_health` | `GET /health` + `/admin/health/all` |
| 1.1 | 写 hot_topics | `tk_hot hot_topics_20260602.json ./hot.json` | `POST /admin/runtime-file/<name>` |
| 1.2 | 写选题卡 + 校验 | `tk_write <p> selection_card.yaml ./sc.yaml` → `tk_select <p>` | `POST /admin/drafts/..` → `/admin/pieces/<p>/select` |
| 2 | 起 X 主稿 | `tk_write <p> xthread_final.md ./x.md` | `POST /admin/drafts/<p>/xthread_final.md` |
| 3 | 4 平台改写 | `tk_adapt <p>` → `tk_wait <task_id>` | `POST /admin/jobs/adapter_orchestrator` |
| 4 | 审稿 | `tk_read <p> xthread_final.md` / `tk_ls <p>` | `GET /admin/drafts/..` |
| 4* | voice 复检 | `tk_voice <p> linkedin_post` | `POST /admin/jobs/voice_checker` |
| 5 | 数据关 过 / 砍 | `tk_state <p> reviewed` / `tk_kill <p>` | `POST /admin/pieces/<p>/state` / `/kill` |
| 6 | 配图 | `tk_img <p> x_main.png ./x_main.png` | `POST /admin/assets/<p>/<file>`（png/jpg/webp/gif，魔数校验） |
| 7 | 短视频 | `tk_video <p>` → `tk_poll <task_id>` | `POST /admin/jobs/mpt_runner` |
| 8 | UTM 短链 | `tk_utm <p> https://taskon.xyz/<lp> <hook>` → `tk_read <p> utm_links.json` | `POST /admin/jobs/utm_generator` |
| 9 | YT 元数据 | `tk_write <p> yt_metadata.yaml ./yt.yaml` | `POST /admin/drafts/<p>/yt_metadata.yaml` |
| 10 | Custom Slice | `tk_slice <p>` | `POST /admin/jobs/custom_slice_generator` |
| 11 | 调度 dry-run | `tk_dryrun <p>` → `tk_wait <task_id>` | `POST /admin/jobs/schedule_planner {dry_run}` |
| 12 | 真发（A 排程 / B 急发） | `tk_schedule <p>` / `tk_publish <p> linkedin_post,yt_shorts 10` | `/admin/jobs/schedule_planner` / `/admin/run_publish` |
| 13 | 记 KOL DM | `tk_logdm <p> @handle reply <tweet_url>` | `POST /admin/jobs/kol_relation_tracker` |

> 异步 job（步 3/7/11/12A）返回 `task_id`，用 `tk_poll <id>` 看一次或 `tk_wait <id>` 阻塞到结束。X Thread 仍是你手发（红线）。

---

## 4 · 安全模型

- 全部端点 Bearer（`ADMIN_API_TOKEN` 空=禁用，安全默认）。token 泄露轮换：改 .env → `docker compose up -d --build` → 旧 token 立失效。
- 路径穿越：`piece_id` / `filename` 正则 + `resolve().relative_to(root)` 双重防御。
- 命令注入：job 用 list-exec（无 shell），参数逐个正则校验；表名取自 `sqlite_master`（非用户输入）。
- 写文件扩展名白名单（禁可执行/二进制）；runtime 根仅允许 `hot_topics_* / candidates_pool_*`。
- 无裸 SQL 端点：state 走枚举校验的 `update_state`，kill 走运行时发现 + 事务级联删。

---

## 5 · 已知边界 / phase2

- ~~步骤 6 配图上传未做~~ **（2026-06-03 已补 phase2）**：`POST /admin/assets/<piece>/<file>` 收 png/jpg/jpeg/webp/gif（魔数校验，落 `drafts/<piece>/<file>` 与 mp4 同级），`MAX_CONTENT_LENGTH` 1→16 MiB，`media_routes` 签名 URL 已加图片后缀供 Postiz 拉取。Cowork 用 `tk_img`。
- **Cowork sandbox 文件视图滞后**：Edit/Write 工具写的是规范文件（Windows 侧），但 Cowork bash 看到的是会话启动时的快照，**bash 看不到本会话内 Edit 的改动**。所以本方案的引擎代码自测是用"快照原文 + 重建补丁"在 /tmp 里跑的；真部署以 Windows 侧规范文件为准。
- **`pieces.state` 无 `killed` 枚举**：手册步 5 的"砍"用 `tk_kill`（硬删 + 级联）实现，不是改 state。

---

## 6 · 为什么不再用方案 B（笔记本守护进程）

方案 B 要在主笔记本常驻一个 FileSystemWatcher + 跑 run_remote.ps1，和 v3 红线"主笔记本只跑 Cowork"冲突，且多一个易挂的移动部件。方案 A 把引擎做成自洽后端，Cowork 纯 HTTP 客户端，符合 v3 架构方向，也彻底干掉了 Z: 这一跳和 5 分钟轮询。
