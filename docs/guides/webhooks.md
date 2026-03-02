# Webhooks

Remembra's webhook system enables real-time integrations with external services. Get notified when memories are created, updated, or deleted.

## Overview

Webhooks allow you to:

- Sync memories to external systems (CRM, analytics, etc.)
- Trigger workflows when new memories are stored
- Build real-time dashboards
- Audit and log all memory operations

## Quick Start

### 1. Register a Webhook

```bash
curl -X POST http://localhost:8787/api/v1/webhooks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "url": "https://your-server.com/webhook",
    "events": ["memory.created", "memory.updated"],
    "secret": "your_webhook_secret"
  }'
```

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
    signature = request.headers.get("X-Remembra-Signature")
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
    print(f"Memory ID: {event['data']['id']}")
    
    return "OK", 200
```

## Available Events

| Event | Description |
|-------|-------------|
| `memory.created` | New memory stored |
| `memory.updated` | Memory content updated |
| `memory.deleted` | Memory deleted |
| `memory.recalled` | Memory accessed via recall |
| `entity.created` | New entity extracted |
| `entity.merged` | Entities merged |

## Webhook Payload

```json
{
  "id": "evt_abc123",
  "type": "memory.created",
  "timestamp": "2026-03-02T12:00:00Z",
  "data": {
    "id": "mem_xyz789",
    "content": "User prefers dark mode",
    "user_id": "user_123",
    "project": "default",
    "metadata": {},
    "created_at": "2026-03-02T12:00:00Z"
  }
}
```

## Security

### HMAC-SHA256 Signature

Every webhook request includes an `X-Remembra-Signature` header:

```
X-Remembra-Signature: sha256=abc123...
```

**Always verify this signature** before processing events to ensure requests come from Remembra.

### Best Practices

1. **Use HTTPS** - Only register HTTPS webhook URLs in production
2. **Verify signatures** - Always validate the HMAC signature
3. **Respond quickly** - Return 2xx within 30 seconds
4. **Handle retries** - Implement idempotency for duplicate events

## Retry Policy

Failed deliveries are retried with exponential backoff:

| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | 1 minute |
| 3 | 5 minutes |
| 4 | 30 minutes |
| 5 | 2 hours |

After 5 failed attempts, the webhook is disabled and you'll receive an email notification.

## API Reference

### Create Webhook

```http
POST /api/v1/webhooks
```

**Request:**
```json
{
  "url": "https://example.com/webhook",
  "events": ["memory.created", "memory.updated"],
  "secret": "your_secret",
  "enabled": true
}
```

### List Webhooks

```http
GET /api/v1/webhooks
```

### Delete Webhook

```http
DELETE /api/v1/webhooks/{webhook_id}
```

### Test Webhook

```http
POST /api/v1/webhooks/{webhook_id}/test
```

Sends a test event to verify your endpoint is working.

## Example: Slack Notifications

```python
import requests

def handle_webhook(event):
    if event["type"] == "memory.created":
        slack_message = {
            "text": f"🧠 New memory stored: {event['data']['content'][:100]}..."
        }
        requests.post(
            "https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK",
            json=slack_message
        )
```

## Troubleshooting

### Webhook not receiving events

1. Check the webhook is enabled: `GET /api/v1/webhooks`
2. Verify the URL is accessible from the internet
3. Check your server logs for incoming requests
4. Use the test endpoint to send a test event

### Signature verification failing

1. Ensure you're using the raw request body (not parsed JSON)
2. Use the exact secret you registered
3. Use `hmac.compare_digest()` for timing-safe comparison
