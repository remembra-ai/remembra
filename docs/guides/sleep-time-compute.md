# Sleep-Time Compute

A background worker that tidies memories between conversations, on the server you run.

## What it does

Each run looks at every account that stored or changed a memory since the previous run (the first run looks
back 24 hours), up to 100 accounts. For each one it runs these passes in order:

1. **Duplicate merge.** Not working yet. For each recent ordinary note it embeds the text and asks the vector
   store for near-duplicates, but that call fails, so it merges nothing. Until it is fixed, each run still
   sends the text of those notes to your embedding provider. With cloud metering on (Remembra Cloud) this pass
   is charged to smart credits and skipped for an account with none left.
2. **Entity alias resolution.** Merges entities of the same type in the same project when one name equals
   the other, is one of its aliases, or contains it ("John" and "John Smith"). Links and relationships move
   to the kept entity; duplicate relationships are closed, not deleted.
3. **Importance rescoring.** For each memory a search has returned, adds 0.05 per return (at most 0.3) to its
   stored importance, up to 1.0. This happens on every run.
4. **Decay cleanup.** Off by default: it deletes nothing unless you turn it on (below).
5. **Themes.** Recomputes the entity communities (themes) of each project.

It never deletes a handoff, checkpoint, status value, source record (the original text a note's facts came
from) or pinned memory, and never changes their text.

## Configuration

| Variable | Default | What it does |
|----------|---------|--------------|
| `REMEMBRA_SLEEP_TIME_ENABLED` | `true` | Start the worker |
| `REMEMBRA_SLEEP_TIME_TRIGGER` | `interval` | `interval` runs it on a timer. Any other value means no timed runs; use the endpoint below |
| `REMEMBRA_SLEEP_TIME_INTERVAL_HOURS` | `6` | Hours between timed runs. The first run is one interval after the server starts |
| `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_ENABLED` | `false` | Let decay cleanup delete old notes nobody recalled |
| `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_DAYS` | `90` | How old such a note must be before decay cleanup deletes it |

## Decay cleanup

With `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_ENABLED=false` (the default) the worker deletes no memories.

With it set to `true`, each run deletes up to 100 of an active account's ordinary notes and facts that:

- were created more than `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_DAYS` days ago,
- have never been returned by a search, and
- have no expiry set.

Handoffs, checkpoints, status values, source records and pinned memories are never deleted, whatever their
age. Pin a note (`POST /api/v1/memories/{id}/pin`) to keep it out of the cleanup.

Each delete is the same as a user's delete: the memory, its entity links, the relationships pulled from it,
its full-text entry and its vector all go. Each one also writes a `memory_decayed` entry to the security log,
and the run logs `decay_cleanup_deleted` with the account and the memory ids.

If you run a hosted service, tell your users before you turn this on: it deletes their notes without asking.

## Endpoints

### Run it now

```http
POST /api/v1/admin/sleep-time/run?user_id=user_123
```

Needs the admin role; once a minute. A tenant admin can run it for their own account only; a superadmin can name
any account, or leave `user_id` out to run every active account. It answers 503 when the worker is off.

```json
{
  "status": "completed",
  "started_at": "2026-09-27T12:00:00",
  "completed_at": "2026-09-27T12:00:02",
  "stats": {
    "memories_scanned": 120,
    "duplicates_merged": 0,
    "entities_resolved": 3,
    "relationships_discovered": 0,
    "importance_rescored": 14,
    "memories_decayed": 0
  },
  "errors": []
}
```

`relationships_discovered` is always 0: no pass finds new relationships yet.

### Status

```http
GET /api/v1/admin/sleep-time/status
```

```json
{
  "enabled": true,
  "decay_cleanup_enabled": false,
  "running": false,
  "last_run": "2026-09-27T06:00:00"
}
```

## Related

- [Conversation Ingestion](./conversation-ingestion.md)
- [Entity Resolution](./entity-resolution.md)
- [Security](./security.md)
