# Role-Based Access Control (RBAC)

Remembra includes a flexible RBAC system for controlling access to memories and administrative functions.

## Overview

RBAC enables:

- **Multi-user deployments** with different permission levels
- **API key scoping** to limit what each key can do
- **Audit compliance** with role-based access logs
- **Enterprise security** requirements

## Roles

Remembra provides three built-in roles:

| Role | Description | Use Case |
|------|-------------|----------|
| `admin` | Full access to all features | System administrators |
| `editor` | Create, read, update memories | Application backends |
| `viewer` | Read-only access | Analytics, dashboards |

## Permissions

### Memory Permissions

| Permission | Admin | Editor | Viewer |
|------------|:-----:|:------:|:------:|
| `memory:create` | ✅ | ✅ | ❌ |
| `memory:read` | ✅ | ✅ | ✅ |
| `memory:update` | ✅ | ✅ | ❌ |
| `memory:delete` | ✅ | ✅ | ❌ |

### Entity Permissions

| Permission | Admin | Editor | Viewer |
|------------|:-----:|:------:|:------:|
| `entity:read` | ✅ | ✅ | ✅ |
| `entity:update` | ✅ | ✅ | ❌ |
| `entity:merge` | ✅ | ❌ | ❌ |

### Admin Permissions

| Permission | Admin | Editor | Viewer |
|------------|:-----:|:------:|:------:|
| `webhook:manage` | ✅ | ❌ | ❌ |
| `audit:read` | ✅ | ❌ | ❌ |
| `user:manage` | ✅ | ❌ | ❌ |
| `settings:manage` | ✅ | ❌ | ❌ |

## Creating Scoped API Keys

### Via API

```bash
curl -X POST http://localhost:8787/api/v1/admin/keys \
  -H "X-API-Key: your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Backend Service",
    "role": "editor",
    "expires_at": "2027-01-01T00:00:00Z"
  }'
```

**Response:**
```json
{
  "id": "key_abc123",
  "key": "rem_sk_live_...",
  "name": "Backend Service",
  "role": "editor",
  "permissions": ["memory:create", "memory:read", "memory:update", "memory:delete", "entity:read", "entity:update"],
  "expires_at": "2027-01-01T00:00:00Z",
  "created_at": "2026-03-02T12:00:00Z"
}
```

### Via Dashboard

1. Navigate to **Settings** → **API Keys**
2. Click **Create Key**
3. Select the role
4. Set an optional expiration date
5. Copy the key (shown only once)

## Using Scoped Keys

Pass the API key in requests:

```python
from remembra import Memory

# Editor key - can store and recall
memory = Memory(
    base_url="http://localhost:8787",
    api_key="rem_sk_live_editor_...",
    user_id="user_123"
)

memory.store("User feedback: Great product!")  # ✅ Works
memory.recall("feedback")  # ✅ Works
```

```python
# Viewer key - read only
memory = Memory(
    base_url="http://localhost:8787",
    api_key="rem_sk_live_viewer_...",
    user_id="user_123"
)

memory.recall("feedback")  # ✅ Works
memory.store("New data")  # ❌ 403 Forbidden
```

## Permission Errors

When a key lacks permission, you'll receive:

```json
{
  "error": "forbidden",
  "message": "Permission denied: memory:create required",
  "required_permission": "memory:create",
  "role": "viewer"
}
```

## Custom Permissions

For advanced use cases, you can create keys with custom permission sets:

```bash
curl -X POST http://localhost:8787/api/v1/admin/keys \
  -H "X-API-Key: your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Analytics Service",
    "permissions": ["memory:read", "entity:read", "audit:read"]
  }'
```

## Audit Logging

All RBAC-protected operations are logged:

```bash
curl http://localhost:8787/api/v1/admin/audit \
  -H "X-API-Key: your_admin_key"
```

**Response:**
```json
{
  "events": [
    {
      "id": "audit_xyz",
      "timestamp": "2026-03-02T12:00:00Z",
      "action": "memory:create",
      "key_id": "key_abc123",
      "role": "editor",
      "user_id": "user_123",
      "resource_id": "mem_456",
      "success": true
    }
  ]
}
```

## Best Practices

1. **Principle of Least Privilege** - Give each key only the permissions it needs
2. **Rotate keys regularly** - Set expiration dates and rotate before expiry
3. **Use separate keys per service** - Makes revocation easier
4. **Monitor audit logs** - Watch for unusual access patterns
5. **Never share admin keys** - Use scoped keys for integrations
