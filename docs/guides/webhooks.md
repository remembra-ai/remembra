# Webhooks

Remembra can send an HTTP POST to your URL when memories are stored, recalled or deleted.

## Overview

Webhooks let you:

- Sync new memories to external systems (CRM, analytics, etc.)
- Trigger workflows when new memories are stored
- Count recalls and deletions

Webhooks are off by default on a self-hosted server. Turn them on with `REMEMBRA_WEBHOOKS_ENABLED=true`; while
they are off, the webhook routes answer `503`.

Managing webhooks needs `webhook:manage` (admin and editor keys, and dashboard sign-ins). A key limited to
projects or bound to one agent is refused, because a webhook receives events from the whole account.

## Quick Start

### 1. Register a Webhook

```bash
curl -X POST http://localhost:8787/api/v1/webhooks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "url": "https://your-server.com/webhook",
    "events": ["memory.stored", "memory.deleted"],
    "secret": "your_webhook_secret"
  }'
```

The URL must be `http` or `https` and must resolve to a public address. Local and private addresses are refused,
at registration and again at each delivery.

### 2. Handle Webhook Events

```python
from flask import Flask, request
import hmac
import hashlib

app = Flask(__name__)
WEBHOOK_SECRET = "your_webhook_secret"

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    # Verify signature
    signature = request.headers.get("X-Remembra-Signature", "")
    payload = request.get_data()

    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(f"sha256={expected}", signature):
        return "Invalid signature", 401

    # Process event
    event = request.json
    print(f"Received: {event['type']}")
    print(f"Data: {event['data']}")

    return "OK", 200
```

## Available Events

| Event | Sent when | `data` |
|-------|-----------|--------|
| `memory.stored` | A store through `POST /api/v1/memories` creates a memory (a duplicate store sends nothing) | `memory_id`, `extracted_facts`, `entities` (names) |
| `memory.recalled` | A recall runs through `POST /api/v1/memories/recall` | `query`, `result_count` |
| `memory.deleted` | Memories are deleted through `DELETE /api/v1/memories` | `memory_id` (or null), `deleted_count` |

Use `*` in `events` to subscribe to all of them. `entity.created` and `entity.merged` are accepted as event
names, but the server does not send them yet. There is no event for a memory update.
`GET /api/v1/webhooks/events/types` lists the names.

## Webhook Payload

```json
{
  "id": "5f0c1b2e-...",
  "type": "memory.stored",
  "timestamp": "2026-03-02T12:00:00+00:00",
  "user_id": "user_123",
  "project_id": "default",
  "data": {
    "memory_id": "mem_xyz789",
    "extracted_facts": ["User prefers dark mode"],
    "entities": []
  }
}
```

Each request also carries `X-Remembra-Event` (the event type) and `X-Remembra-Delivery` (the delivery id).

## Security

### HMAC-SHA256 Signature

A webhook registered with a `secret` gets an `X-Remembra-Signature` header: `sha256=` and the HMAC-SHA256 of the
raw body, keyed with the secret.

```
X-Remembra-Signature: sha256=abc123...
```

Without a secret, no signature is sent. **Register a secret and verify the signature** before processing events.

### Best Practices

1. **Use HTTPS** - Only register HTTPS webhook URLs in production
2. **Verify signatures** - Always validate the HMAC signature
3. **Respond quickly** - Return 2xx within 10 seconds, the default delivery timeout (`REMEMBRA_WEBHOOK_TIMEOUT`)
4. **Handle retries** - Implement idempotency for duplicate events

## Retry Policy

A delivery that gets no 2xx answer is tried 3 times in total (`REMEMBRA_WEBHOOK_MAX_RETRIES`), waiting 2 seconds
and then 4 seconds between tries. After that it is recorded as failed in the webhook's delivery history
(`GET /api/v1/webhooks/{webhook_id}/deliveries`). The webhook is not disabled, and no email is sent.

## API Reference

### Create Webhook

```http
POST /api/v1/webhooks
```

**Request:**
```json
{
  "url": "https://example.com/webhook",
  "events": ["memory.stored", "memory.deleted"],
  "secret": "your_secret"
}
```

### List Webhooks

```http
GET /api/v1/webhooks
```

### Get, Change or Delete a Webhook

```http
GET /api/v1/webhooks/{webhook_id}
PATCH /api/v1/webhooks/{webhook_id}
DELETE /api/v1/webhooks/{webhook_id}
```

`PATCH` takes `url`, `events` and `active`. Set `"active": false` to pause a webhook.

### Delivery History

```http
GET /api/v1/webhooks/{webhook_id}/deliveries
```

There is no test endpoint. To check your receiver, store a memory and look at the delivery history.

## Example: Slack Notifications

```python
import requests

def handle_webhook(event):
    if event["type"] == "memory.stored":
        facts = "; ".join(event["data"]["extracted_facts"])
        slack_message = {
            "text": f"New memory stored: {facts[:100]}"
        }
        requests.post(
            "https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK",
            json=slack_message
        )
```

## Troubleshooting

### Webhook not receiving events

1. Check that webhooks are enabled on the server (`REMEMBRA_WEBHOOKS_ENABLED=true`) and that the webhook is
   active: `GET /api/v1/webhooks`
2. Verify the URL is reachable from the internet and resolves to a public address
3. Check your server logs for incoming requests
4. Read the delivery history: `GET /api/v1/webhooks/{webhook_id}/deliveries`

### Signature verification failing

1. Ensure you're using the raw request body (not parsed JSON)
2. Use the exact secret you registered
3. Use `hmac.compare_digest()` for timing-safe comparison
