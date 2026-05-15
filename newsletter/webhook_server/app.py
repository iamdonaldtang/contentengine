"""
TaskOn Newsletter Webhook Server (v1.2 · 开发vs生产分离最终版)
==============================================================
接 Listmonk 4 类事件 + AWS SES SNS（M4 切换后启用）
   ↓ HTTP 转发到 Donald 桌面 ingestion endpoint
   ↓ 失败 → 写本地 JSONL（兼职女生补抓）+ Lark Webhook 告警

部署：
  - VPS 生产: /opt/taskon-newsletter/webhook_server/
  - 本地开发: E:\AILife\listmonk\webhook_server/（仅试改 / 不真转发）

监听：0.0.0.0:5050
公网：通过 nginx 反代到 https://newsletter-wh.taskon.xyz/ (VPS)
     或 cloudflare tunnel（本地开发）

设计原则：
  - 无状态（事件来 → 转发 → 不存）
  - 失败重试（指数退避 30s/5min/30min）
  - 防伪签名校验（WEBHOOK_SHARED_SECRET）
  - 失败兜底：写 JSONL append-only log（事后补抓）
"""

import os
import json
import logging
import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

# ============================================================
# 环境变量
# ============================================================
DONALD_DESKTOP_INGESTION_URL = os.environ.get('DONALD_DESKTOP_INGESTION_URL', '')
WEBHOOK_SHARED_SECRET = os.environ.get('WEBHOOK_SHARED_SECRET', 'change-me-in-prod')
LARK_WEBHOOK_URL = os.environ.get('LARK_WEBHOOK_URL', '')
FAILED_EVENTS_JSONL = os.environ.get('FAILED_EVENTS_JSONL', '/app/data/failed_events.jsonl')

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# Flask
# ============================================================
app = Flask(__name__)


# ============================================================
# 签名 & 转发
# ============================================================
def sign_payload(body: bytes) -> str:
    """HMAC-SHA256 签名 / 与 ingestion endpoint 共享 secret 验证"""
    return hmac.new(WEBHOOK_SHARED_SECRET.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(req) -> bool:
    """Listmonk webhook 没原生签名，用 Header 校验或 IP 白名单"""
    received = req.headers.get('X-TaskOn-Signature', '')
    if not received:
        # 内网调用（同 Docker 网络）允许跳过
        if req.remote_addr.startswith(('127.', '172.', '10.')):
            return True
        return False
    body = req.get_data()
    expected = sign_payload(body)
    return hmac.compare_digest(received, expected)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=30, min=30, max=1800),
    reraise=True
)
def forward_to_desktop(event: dict) -> None:
    """转发 event 到 Donald 桌面 ingestion endpoint
    重试策略：30s → 5min → 30min（指数退避）
    """
    if not DONALD_DESKTOP_INGESTION_URL:
        raise RuntimeError("DONALD_DESKTOP_INGESTION_URL not configured")

    body = json.dumps(event).encode()
    response = requests.post(
        DONALD_DESKTOP_INGESTION_URL,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'X-TaskOn-Signature': sign_payload(body)
        },
        timeout=15
    )
    response.raise_for_status()


def write_to_jsonl(event: dict) -> None:
    """转发失败时写本地 JSONL，事后补抓"""
    try:
        Path(FAILED_EVENTS_JSONL).parent.mkdir(parents=True, exist_ok=True)
        with open(FAILED_EVENTS_JSONL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
        logger.warning(f"Event written to JSONL fallback: {event.get('event_type')}")
    except Exception as e:
        logger.critical(f"JSONL fallback also failed: {e}")


def alert_lark(severity: str, message: str, details: dict = None) -> None:
    """Lark Webhook 告警"""
    if not LARK_WEBHOOK_URL:
        return
    color = {'P0': 'red', 'P1': 'orange', 'P2': 'yellow'}.get(severity, 'blue')
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"content": f"[{severity}] Newsletter Webhook", "tag": "plain_text"},
                "template": color
            },
            "elements": [
                {"tag": "div",
                 "text": {"content": f"**{message}**\n\n```\n{json.dumps(details or {}, indent=2, ensure_ascii=False)[:1000]}\n```",
                          "tag": "lark_md"}}
            ]
        }
    }
    try:
        requests.post(LARK_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Lark alert failed: {e}")


def process_event(event: dict, severity_on_fail: str = 'P1') -> tuple:
    """统一处理：转发 → 失败写 JSONL + 告警"""
    try:
        forward_to_desktop(event)
        return ('ok', 200)
    except RetryError as e:
        logger.error(f"Forward failed after retries: {e}")
        write_to_jsonl(event)
        alert_lark(severity_on_fail, f'{event.get("event_type")} 转发失败 3 次',
                   {'event': event})
        return ('queued', 200)   # 仍返回 200 防 Listmonk 重复推送
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        write_to_jsonl(event)
        alert_lark(severity_on_fail, f'{event.get("event_type")} 处理异常',
                   {'event': event, 'error': str(e)})
        return ('error', 500)


# ============================================================
# 路由 · Listmonk 4 类事件
# ============================================================

@app.route('/listmonk/campaign_send', methods=['POST'])
def listmonk_campaign_send():
    if not verify_signature(request):
        return jsonify({'error': 'invalid signature'}), 401
    data = request.json or {}
    event = {
        'event_type': 'campaign.send',
        'source': 'listmonk',
        'campaign_id': data.get('id'),
        'subject': data.get('subject'),
        'sent_count': data.get('sent'),
        'timestamp': datetime.utcnow().isoformat()
    }
    status, code = process_event(event, 'P1')
    return jsonify({'status': status}), code


@app.route('/listmonk/open', methods=['POST'])
def listmonk_open():
    if not verify_signature(request):
        return jsonify({'error': 'invalid signature'}), 401
    data = request.json or {}
    event = {
        'event_type': 'campaign.open',
        'source': 'listmonk',
        'subscriber_email': data.get('subscriber', {}).get('email'),
        'subscriber_uuid': data.get('subscriber', {}).get('uuid'),
        'campaign_id': data.get('campaign', {}).get('id'),
        'timestamp': datetime.utcnow().isoformat()
    }
    # Open 事件量大，失败不告警（只写 JSONL）
    status, code = process_event(event, 'P2')
    return jsonify({'status': status}), code


@app.route('/listmonk/click', methods=['POST'])
def listmonk_click():
    if not verify_signature(request):
        return jsonify({'error': 'invalid signature'}), 401
    data = request.json or {}
    event = {
        'event_type': 'campaign.click',
        'source': 'listmonk',
        'subscriber_email': data.get('subscriber', {}).get('email'),
        'subscriber_uuid': data.get('subscriber', {}).get('uuid'),
        'campaign_id': data.get('campaign', {}).get('id'),
        'url': data.get('url'),
        'timestamp': datetime.utcnow().isoformat()
    }
    status, code = process_event(event, 'P2')
    return jsonify({'status': status}), code


@app.route('/listmonk/bounce', methods=['POST'])
def listmonk_bounce():
    if not verify_signature(request):
        return jsonify({'error': 'invalid signature'}), 401
    data = request.json or {}
    event = {
        'event_type': 'subscriber.bounced',
        'source': 'listmonk',
        'subscriber_email': data.get('email'),
        'subscriber_uuid': data.get('uuid'),
        'bounce_type': data.get('type'),
        'reason': data.get('source'),
        'timestamp': datetime.utcnow().isoformat()
    }
    status, code = process_event(event, 'P1')
    # 硬 bounce 额外 P2 告警
    if event.get('bounce_type') == 'hard':
        alert_lark('P2', f"硬 bounce: {event['subscriber_email']}", event)
    return jsonify({'status': status}), code


# ============================================================
# 路由 · AWS SES SNS（M4 切换后启用）
# ============================================================

@app.route('/ses/sns', methods=['POST'])
def ses_sns():
    raw = request.get_data(as_text=True)
    try:
        msg = json.loads(raw)
    except Exception:
        return jsonify({'error': 'invalid json'}), 400

    if msg.get('Type') == 'SubscriptionConfirmation':
        confirm_url = msg.get('SubscribeURL')
        logger.info(f"SNS SubscriptionConfirmation, URL: {confirm_url}")
        alert_lark('P2', 'SES SNS 待手动确认', {'confirm_url': confirm_url})
        return jsonify({'note': 'manually visit SubscribeURL to confirm'}), 200

    if msg.get('Type') != 'Notification':
        return jsonify({'status': 'ignored'}), 200

    try:
        notification = json.loads(msg.get('Message', '{}'))
    except Exception:
        notification = {}

    ntype = notification.get('notificationType')

    if ntype == 'Bounce':
        bounce = notification.get('bounce', {})
        for r in bounce.get('bouncedRecipients', []):
            event = {
                'event_type': 'ses.bounce',
                'source': 'ses_sns',
                'subscriber_email': r.get('emailAddress'),
                'bounce_type': bounce.get('bounceType'),
                'sub_type': bounce.get('bounceSubType'),
                'diagnostic': r.get('diagnosticCode'),
                'timestamp': datetime.utcnow().isoformat()
            }
            process_event(event, 'P2')
            if bounce.get('bounceType') == 'Permanent':
                alert_lark('P2', f"SES 硬 bounce: {r.get('emailAddress')}", event)

    elif ntype == 'Complaint':
        complaint = notification.get('complaint', {})
        for r in complaint.get('complainedRecipients', []):
            event = {
                'event_type': 'ses.complaint',
                'source': 'ses_sns',
                'subscriber_email': r.get('emailAddress'),
                'feedback_type': complaint.get('complaintFeedbackType'),
                'timestamp': datetime.utcnow().isoformat()
            }
            process_event(event, 'P0')   # Complaint 是 P0
            alert_lark('P0', f"SES Complaint! {r.get('emailAddress')}", event)

    return jsonify({'status': 'ok'}), 200


# ============================================================
# 健康检查
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    # 测试是否可达 Donald 桌面 ingestion endpoint
    ingestion_ok = False
    if DONALD_DESKTOP_INGESTION_URL:
        try:
            health_url = DONALD_DESKTOP_INGESTION_URL.rsplit('/', 1)[0] + '/health'
            r = requests.get(health_url, timeout=5)
            ingestion_ok = r.status_code == 200
        except Exception:
            ingestion_ok = False

    # JSONL fallback 文件可写检查
    fallback_ok = True
    try:
        Path(FAILED_EVENTS_JSONL).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        fallback_ok = False

    healthy = ingestion_ok and fallback_ok
    return jsonify({
        'status': 'ok' if healthy else 'degraded',
        'service': 'taskon-newsletter-webhook',
        'ingestion_endpoint_reachable': ingestion_ok,
        'fallback_jsonl_writable': fallback_ok
    }), 200 if healthy else 503


# ============================================================
# Entrypoint
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)
