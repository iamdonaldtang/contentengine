# 运维 Runbook · TaskOn Newsletter Engine

> **本文档定位**：日常运维 + 异常处理的 SOP。按异常等级（P0/P1/P2）分组。

---

## 日常运维清单

### 每周一（兼职女生 30min）

```
□ VPS docker compose ps        # 4 容器都 healthy
□ Listmonk Admin UI 可登入（https://newsletter.taskon.xyz/admin）
□ Donald 桌面 ingestion endpoint Windows Service 跑着（nssm status）
□ Cloudflare Tunnel 状态正常（Get-Service cloudflared）
□ SendGrid Dashboard 看上周用量（< 80% 月度上限）
□ SES Console 看 Reputation Dashboard（如已切 SES）
□ DKIM/SPF/DMARC 是否仍通过（mxtoolbox 自动检查）
□ VPS Postgres backup 成功（上周日 23:00 cron）
□ VPS /opt/taskon-newsletter/backups/ 备份文件数 ≥ 4
□ VPS webhook 转发失败 JSONL 是否有积压
```

### 每周日 23:00（VPS cron 自动）

```bash
# /etc/cron.d/listmonk-backup
0 23 * * 0 cd /opt/taskon-newsletter && \
  docker compose exec -T listmonk-db pg_dump -U listmonk listmonk | \
  gzip > /opt/taskon-newsletter/backups/listmonk_$(date +\%F).sql.gz

# 删 30 天前的备份
0 0 * * * find /opt/taskon-newsletter/backups/ -name "*.sql.gz" -mtime +30 -delete
```

### Donald 桌面端备份

```powershell
# SQLite state.db 每日自动备份（由 v3 其他 engine/ 模块共享备份脚本负责）
# 见 engine/scripts/backup_sqlite.py
```

### 每月（兼职女生 1h）

```
□ 删除 30 天前的 Postgres backup（保留近 4 周）
□ Listmonk 升级（拉新 image + restart）
□ webhook_server 升级（python 依赖）
□ Newsletter campaign 历史归档（>3 个月的 archive 标记）
□ Soft bounce 超 3 次的邮箱手动检查（可能是临时错误，可恢复）
□ 检查 unsubscribe 趋势（>0.5% 立刻警惕）
```

---

## 异常分级响应

### P0 立即（<2h）

| 异常 | 现象 | 处理 SOP |
|---|---|---|
| **AWS SES 账号被 suspend** | Bounce >0.5% 或 Complaint >0.1% | 1. 立即在 Listmonk Settings → SMTP 改回 SendGrid (W1 阶段) 或 Postmark (M4+ fallback)<br>2. SES Console 提交申诉<br>3. 清洗 bounce 邮箱<br>4. 暂停所有 campaign 7 天 |
| **SendGrid 100/天上限耗尽** | 19:00 后邮件全部 5xx | 1. 立即暂停自动发送任务<br>2. 当日剩余邮件改到次日 09:00 发<br>3. 长期解：升 SendGrid Essentials $19.95/月 或加速 M4 切 SES |
| **Listmonk 服务宕机** | 502 Bad Gateway / 不响应 | 1. SSH 登 VPS / `docker compose restart listmonk`<br>2. 失败 → `docker compose logs --tail=200 listmonk`<br>3. 数据库连不上 → 重启 listmonk-db<br>4. 全失败 → 从 Postgres backup 重建 |
| **VPS 整体宕机** | 所有服务 unreachable | 1. VPS 控制台看网络/主机故障<br>2. 硬件故障 → 从 Postgres backup + git clone 重建到新 VPS（预计 2h）|
| **Donald 桌面 ingestion endpoint 失联** | webhook_server 转发持续失败 | 1. Donald 桌面 `nssm status TaskOnNewsletterIngestion` 看 service<br>2. `Get-Service cloudflared` 看 tunnel<br>3. 重启 service：`nssm restart TaskOnNewsletterIngestion`<br>4. 期间事件 buffer 在 VPS `data/webhook/failed_events.jsonl`，恢复后兼职女生补抓 |
| **Donald 桌面整机关机** | ingestion 失联 / webhook 全部转发失败积压 | 1. 重启桌面后 service 自动起<br>2. VPS 检查 `failed_events.jsonl` 大小<br>3. 兼职女生跑补抓脚本（待 W4 补） |
| **VPS webhook 转发失败率 >10%** | 看 `failed_events.jsonl` 增长 | 检查 Donald 桌面 ingestion + Cloudflare Tunnel + HMAC secret 一致 |
| **Nginx 证书过期** | HTTPS 浏览器报错 | jonasal/nginx-certbot 自动续；检查 `docker compose logs nginx` |
| **Cloudflare Tunnel 断** | VPS 调 ingestion 全部超时 | Donald 桌面 `Restart-Service cloudflared` + 看日志 |

### P1 当天（<8h 工作日）

| 异常 | 现象 | 处理 SOP |
|---|---|---|
| **单条 campaign Bounce Rate 高（2-5%）** | Listmonk campaign 详情 | 暂停剩余发送，清洗本批次邮箱 |
| **Unsubscribe Rate >1%** | 单条 campaign | 立即检查内容是否过度商业化（R9 红线）|
| **Webhook 转发 24h 失败率 >10%** | webhook_server 日志 | 检查 Donald 桌面 ingestion 是否稳定 |
| **DKIM/SPF/DMARC 偶尔 FAIL** | Gmail Show original | 检查 DNS 是否被改 / 是否有 propagation 延迟 |
| **Open Rate 跌 30%+** | 周环比 | 检查发件时段 / 内容质量 / IP 信誉变化 |
| **Listmonk admin GUI 慢** | >5s 响应 | docker stats 看资源；可能 Postgres 需 VACUUM |

### P2 次日（<24h）

| 异常 | 处理 |
|---|---|
| 单个订阅者 soft bounce 1-2 次 | Listmonk 自动重试 / 不告警 |
| 个别 campaign click 事件偶尔丢 | webhook 重试机制兜底 |
| Listmonk 升级 minor 版本 | 测试环境验证后再升 |
| 兼职女生忘记发 weekly digest | 第 2 天补发 |

---

## 备份与恢复

### 备份策略

```
- Postgres: 每周日 23:00 全量 SQL dump，gzip 压缩，保留 4 周
- Listmonk uploads: rsync 每周日到 Cloudflare R2（$1/月 / 1GB）
- 配置文件: git 管理（.env 除外）
```

### 恢复演练（每季度 1 次）

#### 场景 A · VPS Listmonk 数据丢失，VPS 仍在

```bash
ssh taskon@<vps-ip>
cd /opt/taskon-newsletter

# 1. 停所有容器
docker compose down

# 2. 删 Postgres 数据
sudo rm -rf data/postgres

# 3. 重新启动空 db
docker compose up -d listmonk-db
sleep 30

# 4. 恢复最新 dump
gunzip < backups/listmonk_2026-05-26.sql.gz | \
    docker compose exec -T listmonk-db psql -U listmonk listmonk

# 5. 启动所有
docker compose --profile prod up -d
```

#### 场景 B · VPS 整机故障，迁新 VPS

```bash
# 1. 在新 VPS 装 Docker + docker-compose v2
# （同部署手册 D2 步骤）

# 2. clone 代码
sudo mkdir -p /opt/taskon-newsletter
cd /opt/taskon-newsletter
git clone git@github.com:taskon-org/listmonk.git .

# 3. 把 .env + config/config.toml 从备份恢复
# （Lark 密码本 / 1Password 共享）

# 4. 把 Postgres dump 从对象存储下载
# (假设备份 sync 到 Cloudflare R2 / AWS S3 / B2)
rsync user@backup-server:/path/to/listmonk_2026-05-26.sql.gz backups/

# 5. 启动空 db + 恢复
docker compose up -d listmonk-db
sleep 30
gunzip < backups/listmonk_2026-05-26.sql.gz | \
    docker compose exec -T listmonk-db psql -U listmonk listmonk

# 6. 启动所有
docker compose --profile prod up -d

# 7. 改 DNS 把 newsletter.taskon.xyz 指向新 VPS IP

# 8. 改 Donald 桌面 ingestion endpoint 的 WEBHOOK_SHARED_SECRET（保持原值即可，新 VPS .env 写一样的）
```

#### 场景 C · Donald 桌面 SQLite 损坏

```powershell
# 从每日备份恢复（v3 其他模块的 backup_sqlite.py）
cd D:\Taskon\marketing\00_内容营销引擎\runtime
copy state.db state.db.broken
copy state.db.bak.<昨天日期> state.db
```

---

## 监控指标（每日扫一遍）

| 指标 | 来源 | 健康阈值 | 警戒 |
|---|---|---|---|
| 容器健康 | docker-compose ps | 4/4 healthy | <4 立即查 |
| Webhook 成功率 | webhook_server log | >99% | <95% P1 |
| SendGrid 日发送量 | SendGrid Dashboard | <90/天（W1-M2）| >100 切日 |
| SES Reputation | SES Console (M4+) | Bounce <0.5% / Complaint <0.1% | 任一超立即 P0 |
| Newsletter Open Rate | Listmonk | >25%（warm-up 后）| <20% P1 |
| Unsubscribe Rate | Listmonk | <0.5% | >1% P1 |
| Postgres 大小 | docker stats listmonk-db | <500MB（M3） | >1GB 考虑归档 |
| VPS CPU | top / docker stats | <50% avg | >80% 持续 升级 |
| VPS 磁盘 | df -h | <70% | >85% P1 清理 backups |

---

## Lark 告警机制

webhook_server/app.py 内置 Lark Webhook 推送。配置位于 .env 的 `LARK_WEBHOOK_URL`。

测试 Lark 是否通：

```bash
docker-compose exec webhook_server python -c "
from app import alert_lark
alert_lark('P2', 'Lark 告警测试', {'note': 'Newsletter Engine 部署后健康检查'})
"
```

---

## 常用排查命令

### VPS 端（bash / SSH 登入后）

```bash
cd /opt/taskon-newsletter

# 容器日志
docker compose logs -f --tail=200 listmonk
docker compose logs -f --tail=200 webhook_server

# 进容器
docker compose exec listmonk sh
docker compose exec listmonk-db psql -U listmonk

# 重启
docker compose restart listmonk
docker compose --profile prod up -d

# 看失败事件
cat data/webhook/failed_events.jsonl | tail -20
wc -l data/webhook/failed_events.jsonl

# 看资源
docker stats
df -h
docker system df
```

### Donald 桌面端（PowerShell）

```powershell
# ingestion endpoint service 状态
nssm status TaskOnNewsletterIngestion
Get-Service cloudflared

# 重启
nssm restart TaskOnNewsletterIngestion
Restart-Service cloudflared

# 看日志（如 nssm 配了 stdout / stderr 路径）
Get-Content D:\Taskon\marketing\engine\newsletter\webhook_server\ingestion.log -Tail 50 -Wait

# 测试 health
curl http://localhost:5051/health
curl https://taskon-ingestion.taskon.xyz/health

# Cloudflare Tunnel 状态
cloudflared tunnel info taskon-ingestion

# 查 SQLite
cd D:\Taskon\marketing\00_内容营销引擎\runtime
sqlite3 state.db ".tables"
sqlite3 state.db "SELECT count(*) FROM user_journey;"
sqlite3 state.db "SELECT * FROM newsletter_campaigns ORDER BY send_time DESC LIMIT 5;"
```

### E 盘本地开发

```powershell
cd E:\AILife\listmonk
git status
git pull upstream main        # 拉 knadh/listmonk 上游更新
git push origin main          # push 到 taskon-org fork

# 本地试跑（可选）
docker compose up -d
# 访问 http://localhost:9000
```

---

## 升级 Listmonk 流程（VPS bash）

```bash
ssh taskon@<vps-ip>
cd /opt/taskon-newsletter

# 1. 备份当前 db
docker compose exec -T listmonk-db pg_dump -U listmonk listmonk | \
    gzip > backups/pre-upgrade_$(date +%F).sql.gz

# 2. 先在本地 E 盘试改
# 在 E:\AILife\listmonk\ 改 docker-compose.yml 锁定新版本
# docker compose pull + docker compose up -d 本地试跑
# 确认 OK 后 git commit + push

# 3. VPS 端 git pull + 升级
git pull
docker compose pull listmonk
docker compose --profile prod up -d listmonk

# 4. 验证
docker compose logs --tail=50 listmonk
curl https://newsletter.taskon.xyz/api/health
```

如失败回滚：

```bash
# E 盘本地改回旧版本 image tag → git push
# VPS:
git pull
docker compose --profile prod up -d listmonk
```

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-09 早 | 首版（VPS 版） |
| v2.0 | 2026-05-09 深夜（已撤回） | 误改全本地桌面部署 |
| **v1.2** | **2026-05-09 深夜（最终）** | **开发vs生产分离**：VPS 生产（4 容器）+ Donald 桌面 ingestion endpoint + Cloudflare Tunnel + SQLite 在 D 盘；E:\AILife\listmonk\ 仅做开发 |
