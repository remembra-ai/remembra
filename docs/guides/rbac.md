# Roles and Permissions

Every API key has one role: `admin`, `editor` or `viewer`. The role decides which
permissions the key holds. A route answers 403 when the key does not hold the
permission it needs.

## Roles

| Role | What it is for | How a key gets it |
|------|----------------|-------------------|
| `admin` | Every permission, plus the admin routes below | Created with the server's master key; an admin key can then give another of the account's keys the admin role |
| `editor` | Everything except the `admin:*` permissions | Dashboard, or `POST /api/v1/keys` (the default role) |
| `viewer` | Read only | Dashboard, or `POST /api/v1/keys` |

A viewer key is read-only. Every route that changes data refuses it. A test
sends a viewer key to every write route the API serves and fails if one lets it
through.

A dashboard sign-in has the editor permissions.

## Permissions

This table is generated from `src/remembra/auth/rbac.py` (`permission_table()`).
A test fails when it no longer matches the code.

<!-- permission-table:start -->
| Permission | admin | editor | viewer | What it allows |
|---|:---:|:---:|:---:|---|
| `memory:store` | yes | yes | no | Store, change, pin, import and ingest memories; send recall feedback; write inbox messages, relay handoffs and session status; change spaces, teams and project links; recompute the brain layer; start audio capture |
| `memory:recall` | yes | yes | yes | Recall, list, read and export memories; read spaces, inbox messages, relay briefs and trails, conflicts, timelines and brain insights |
| `memory:delete` | yes | yes | no | Delete memories and clean up expired or decayed ones |
| `key:create` | yes | yes | no | Create API keys (never above the caller's own role, never admin) and rename them |
| `key:list` | yes | yes | yes | List the account's API keys |
| `key:revoke` | yes | yes | no | Revoke or delete API keys (an API key only ones with no more access than itself) |
| `webhook:manage` | yes | yes | no | Create, read, change and delete webhooks and read their deliveries |
| `conflict:manage` | yes | yes | no | Resolve or dismiss memory conflicts |
| `entity:read` | yes | yes | yes | Read entities, their relationships and the memories that mention them |
| `admin:audit` | yes | no | no | Read the account's audit log |
| `admin:export` | yes | no | no | Export the account's audit log as JSON or CSV |
| `admin:users` | yes | no | no | Nothing yet: no route checks it |
| `account:manage` | yes | yes | no | Redeem a promo code; email the verification link of an account created by API signup |
<!-- permission-table:end -->

`admin:users` is defined but no route checks it yet.

Admin keys also pass the routes that require the admin role:
`GET`/`POST /api/v1/admin/roles`, `DELETE /api/v1/admin/roles/{api_key_id}`,
`GET /api/v1/admin/permissions` and `POST /api/v1/admin/sleep-time/run`.

Webhook routes also refuse a key that is limited to projects or bound to one
agent, because webhooks receive events from the whole account.

## Creating keys

### In the dashboard

Open **API Keys**, click **Generate New Key**, pick **Editor** or **Viewer**, and
choose all projects or some. The key is shown once. The dashboard does not
offer **Admin**: the server refuses to create admin keys from a dashboard
sign-in.

### With the API

A dashboard sign-in, or an API key that holds `key:create`, can create keys:

```bash
curl -X POST https://api.remembra.dev/api/v1/keys \
  -H "X-API-Key: $REMEMBRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "CI reader", "role": "viewer", "project_ids": ["my-app"]}'
```

The response holds the new key in `key`. It is shown only once.

An API key can only create keys up to its own role, never `admin`. A key that
is limited to projects can only create keys limited to some of those projects.
If it leaves `project_ids` out, the new key gets the same projects.

### Admin keys

Admin keys are created with the server's master key (`REMEMBRA_AUTH_MASTER_KEY`),
for a given `user_id`:

```bash
curl -X POST http://localhost:8787/api/v1/keys \
  -H "X-API-Key: $REMEMBRA_AUTH_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_123", "name": "Ops", "role": "admin"}'
```

## Changing and revoking keys

- `PATCH /api/v1/keys/{key_id}` with `name` renames a key (needs `key:create`).
  A change of `role` or `project_ids` needs a dashboard sign-in, and never
  grants `admin`.
- `DELETE /api/v1/keys/{key_id}` revokes a key (needs `key:revoke`). Add
  `?hard=true` to delete it. An API key can only revoke keys that hold no
  more access than itself.
- A change of role or projects applies from the key's next request.
- A viewer key cannot rename, revoke or delete any key, not even its own.

## Real-time connections

The real-time endpoint (`/api/v1/ws`, used by the dashboard) needs `memory:recall` and follows the same rules
as every API request:

- Revoking or permanently deleting a key closes every real-time connection opened with it at once, with close
  code `4001` and the reason "Access revoked or expired".
- Signing out closes the connection opened with that sign-in. Changing or resetting your password closes the
  connections of your earlier dashboard sign-ins; connections opened with API keys stay open. Deactivating or
  deleting an account closes all of its connections.
- Before each event is sent, the server checks the connection's key or sign-in again. An idle connection is
  checked every 30 seconds. If access has ended (a revoked key, an ended or expired sign-in, a deactivated
  account), the event is not sent and the connection closes with `4001`. A dashboard sign-in expires 24 hours
  after it began. If the key lost `memory:recall` or the project the connection follows, it closes with `4003`.
  If the check itself fails (a database error, say), the event is not sent and the connection closes with
  `1011`; clients may reconnect.
- No event created after access is cut is sent. An event already being sent at that moment may still arrive.
- The connection is closed at once only by the server process that handled the revocation. With several
  processes, connections on the others get no further events and close within 30 seconds.

## Narrowing a key with scopes

An admin key can set scopes on one of the account's keys. Scopes narrow the
key's role. They never add a permission the role does not hold: a viewer key
with the `memory:store` scope still cannot store.

```bash
curl -X POST https://api.remembra.dev/api/v1/admin/roles \
  -H "X-API-Key: $REMEMBRA_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"api_key_id": "key_abc123", "role": "editor", "scopes": ["memory:recall", "memory:store"]}'
```

The response lists the key's `permissions` after the change. A key cannot be
given a role, scopes or projects beyond those of the admin key that sets them.
A key created by a scoped key gets the same scopes.

## Using keys

```python
import os
from remembra import Memory, MemoryError

reader = Memory(
    base_url="https://api.remembra.dev",
    api_key=os.environ["REMEMBRA_VIEWER_API_KEY"],
    user_id="user_123",
)

reader.recall("feedback")  # works

try:
    reader.store("New data")
except MemoryError as e:
    print(e.status_code)  # 403
```

## Permission errors

A key without the permission gets a 403. The `detail` names what is missing:

```json
{"detail": "Permission denied: memory:store required"}
```

The admin audit routes answer `"Insufficient permissions. Required: admin:export"`
(or `admin:audit`), and the admin role routes answer
`"Role 'admin' or higher required."`

## Audit log

An admin key reads the account's audit log with `GET /api/v1/admin/audit`
(`admin:audit`) and exports it with `GET /api/v1/admin/audit/export/json` or
`/export/csv` (`admin:export`). [SECURITY.md](https://github.com/remembra-ai/remembra/blob/main/SECURITY.md#audit-logging)
lists the events it records.
