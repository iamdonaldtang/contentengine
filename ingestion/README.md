# TaskOn Marketing Engine · Ingestion Service

Flask HTTP service exposing the public webhook + landing-signup surface for
the content marketing engine. Runs as a sibling container next to the batch
`engine` service; both share the same SQLite `state.db` via a host bind
mount.

```
External HTTPS → Cloudflare Tunnel → ingestion:5051 (Flask + gunicorn)
                                          ↓
                                     /app/runtime/state.db  ←  engine batch jobs
```

---

## Endpoint surface

| Method | Path                     | Purpose                                                  |
|--------|--------------------------|----------------------------------------------------------|
| POST   | `/api/landing-signup`    | Landing page form submissions (UTM-tagged)               |
| POST   | `/api/listmonk-webhook`  | Listmonk Open / Click / Bounce events                    |
| POST   | `/api/ses-bounce`        | AWS SNS envelope: SES Bounce / Complaint                 |
| GET    | `/health`                | Liveness probe — verifies SQLite reachability            |
| GET    | `/metrics`               | Prometheus-format gauge metrics (row counts + heartbeat) |

All POST endpoints accept JSON and return JSON
(`{"status": "ok", ...}` on success, `{"status": "error", "code": ..., "message": ...}` on
failure). The request body is capped at **1 MiB** (`MAX_CONTENT_LENGTH`).

---

## Request / response examples

### Landing signup

```bash
curl -sS -X POST http://localhost:5051/api/landing-signup \
  -H 'Content-Type: application/json' \
  -d '{
    "email": "founder@somedex.xyz",
    "page_path": "/benchmark-report",
    "url": "https://taskon.xyz/benchmark-report?utm_source=twitter&utm_medium=thread&utm_campaign=2026w19_thread01&utm_content=donald_en&utm_term=47pct_bot",
    "referrer": "https://twitter.com/i/web/status/123",
    "cookie_id": "anon_cookie_abc123"
  }'
# → 201 {"status":"ok","lead_id":42,"is_new":true}
```

Second call with the same `email` → `200 {"is_new": false}` (UPSERT bumps
`last_seen_at` but does not overwrite `first_*`).

### Listmonk webhook

```bash
curl -sS -X POST http://localhost:5051/api/listmonk-webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "event": "campaign.click",
    "subscriber_email": "lead@example.com",
    "campaign_id": 5,
    "timestamp": "2026-05-13T10:23:45Z",
    "url": "https://taskon.xyz/landing?utm_source=listmonk&utm_medium=edm&utm_campaign=2026w20_newsletter&utm_content=donald_en&utm_term=cta"
  }'
```

Supported `event` values: `campaign.open` · `campaign.click` · `campaign.bounce`.
Anything else returns `400 unknown_event`.

### SES bounce

AWS SNS HTTP subscriptions wrap the actual payload in `Message` (JSON string).
The service handles both `SubscriptionConfirmation` (auto-confirms by GET'ing
`SubscribeURL`) and `Notification`:

```bash
curl -sS -X POST http://localhost:5051/api/ses-bounce \
  -H 'Content-Type: application/json' \
  -d '{
    "Type": "Notification",
    "Message": "{\"notificationType\":\"Bounce\",\"bounce\":{\"bounceType\":\"Permanent\",\"bouncedRecipients\":[{\"emailAddress\":\"invalid@example.com\"}]}}"
  }'
```

Permanent bounce → `UPDATE leads SET bounced=1`. Complaint → P0 row in
`publish_failures`.

### Health + metrics

```bash
curl -sS http://localhost:5051/health
# → {"status":"ok","service":"taskon-ingestion"}

curl -sS http://localhost:5051/metrics
# → taskon_leads_total 142
#   taskon_pieces_total 47
#   taskon_heartbeat_last_seconds{job="metrics_collector"} 1234
```

---

## HMAC signature verification

Both `/api/landing-signup` and `/api/listmonk-webhook` support optional
HMAC-SHA256 verification. When the relevant env var is set, requests **without
a valid signature** are rejected with `401 bad_signature`.

| Endpoint                 | Env var                    | Header                  |
|--------------------------|----------------------------|-------------------------|
| `/api/landing-signup`    | `INGESTION_HMAC_SECRET`    | `X-Signature`           |
| `/api/listmonk-webhook`  | `LISTMONK_WEBHOOK_SECRET`  | `X-Listmonk-Signature`  |

Signature is `hex(HMAC-SHA256(secret, raw_body))`. The header value may be
sent raw or prefixed with `sha256=`. Generate one with:

```bash
echo -n '<body>' | openssl dgst -sha256 -hmac '<secret>' -binary | xxd -p -c 256
```

If the env var is empty / unset, HMAC is **not enforced** — useful for local
dogfooding but DO NOT ship that to production.

---

## Deployment

### Docker compose (recommended)

```bash
cd D:/Taskon/marketing/engine
docker compose build ingestion
docker compose up -d engine ingestion
docker compose logs -f ingestion
```

The `engine` and `ingestion` containers share `./runtime` (bind mount) so
both processes see the same `state.db`. Port `127.0.0.1:5051` is mapped to
the host for local `curl` smoke tests — production traffic comes via
Cloudflare Tunnel.

### Cloudflare Tunnel

Why a tunnel instead of opening a port:

* No public IP / no inbound firewall hole — TaskOn's home / co-located host
  does not expose 80/443
* DDoS protection + WAF for free
* Cert rotation handled by Cloudflare
* The container speaks plain HTTP — no certificate gymnastics

Connector config (single named tunnel, shared with the rest of TaskOn infra):

```yaml
# /etc/cloudflared/config.yml
tunnel: taskon-ingestion
credentials-file: /etc/cloudflared/taskon-ingestion.json
ingress:
  - hostname: ingest.taskon.xyz
    service: http://ingestion:5051
  - service: http_status:404
```

Then in the Cloudflare dashboard add the `ingest.taskon.xyz` CNAME to
the tunnel. Verify end-to-end with:

```bash
curl https://ingest.taskon.xyz/health
```

---

## Environment variables

| Name                       | Default  | Purpose                                            |
|----------------------------|----------|----------------------------------------------------|
| `INGESTION_PORT`           | `5051`   | Listen port (dev only — compose maps this)         |
| `INGESTION_HMAC_SECRET`    | _empty_  | If set: enforce `X-Signature` on landing-signup    |
| `LISTMONK_WEBHOOK_SECRET`  | _empty_  | If set: enforce `X-Listmonk-Signature` on webhook  |
| `SQLITE_PATH`              | _auto_   | Override SQLite location (default engine/runtime)  |
| `LOG_LEVEL`                | `INFO`   | `DEBUG` / `INFO` / `WARNING` / `ERROR`             |

Add to `.env`:

```
INGESTION_PORT=5051
INGESTION_HMAC_SECRET=
LISTMONK_WEBHOOK_SECRET=
```

---

## Troubleshooting

**`/health` returns 503**: the `state.db` file is unreachable. Check the
bind mount (`docker compose exec ingestion ls -la /app/runtime`) and that the
`engine` service ran `init_db` first.

**`401 bad_signature` on every request**: check `INGESTION_HMAC_SECRET` is the
same on both sender and ingestion side. Recompute the signature against the
exact bytes you POST (no trailing newline, no Flask-side reformatting).

**`payload_too_large`**: request body exceeds 1 MiB. Trim JSON or raise
`MAX_CONTENT_LENGTH` in `app.py` (PRs welcome).

**Metrics show negative counts**: a count of `-1` means the SQL `COUNT(*)`
itself raised — check container logs for the offending query.

**SES SubscriptionConfirmation fails**: the container needs outbound HTTPS
to `*.sns.<region>.amazonaws.com`. Verify with
`docker compose exec ingestion curl -fsS https://sns.us-east-1.amazonaws.com`.

---

## Hard rules compliance (Prompt §7)

* ✅ No silent failures — every handler logs at WARNING+ on rejection paths
* ✅ No hardcoded secrets / paths — env-driven; `state.db` location via `SQLITE_PATH`
* ✅ No bare `print()` — JSON logging to stdout
* ✅ No direct `sqlite3.connect()` — `from lib.db import db` exclusively
* ✅ `requests.get(...)` calls carry `timeout=10`
* ✅ Full type hints (Python 3.12)
