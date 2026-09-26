# Snapshots, WebSocket frames and the tree snapshot

Source of truth: `remembra.crew.schemas` (`SNAPSHOT`, `LOCAL_SNAPSHOT`, `WS_SUBSCRIBE`,
`WS_FRAMES`, `validate_tree`, `snapshot_hmac`). Spec: §4.4, §8.1, §11.

## Server snapshot — `GET /crews/{id}/snapshot` (`CrewSnapshot`)

| Key | Content |
|---|---|
| `crew` | `CrewView` (`last_seq` = `as_of_seq`) |
| `server_time`, `as_of_seq`, `etag` | server clock; the seq the snapshot is consistent with; ETag for 304 |
| `sessions[]` | `SessionView` (never a token or token hash) |
| `claims[]` | live claims only (`requested, queued, active, offered, reserved`), with `epoch` |
| `zones[]`, `commons[]`, `ignore[]` | compiled zones (`ZoneView` including globs, command patterns, MCP rules), commons `{glob, kind}`, ignore globs |
| `tasks[]` | open tasks (`TaskView`) |
| `collisions[]` | open and acknowledged |
| `decisions[]` | `proposed` and `in_force` (the dashboard shows proposed ones to confirm; agents only ever see in-force ones) |
| `offers[]` | recorded baton offers (`OfferView`), used for the reserved-deny variant (D33) |
| `footprints[]` | live `dirty`/`committed` footprints (`FootprintView`, ≤2,000), used by guard rows 5 and 16 |
| `inbox_counts` | `{project, crew}` counts of live items |
| `pending_zone_changes[]` | ids of pending zone changes |

## Local snapshot — `~/.remembra/crew/snapshot/<crew>.json` (`LocalSnapshot`)

Everything above plus:

| Key | Content |
|---|---|
| `synced_at` | server time of the sync |
| `skew_s` | measured clock skew (fail-closed decisions refuse `|skew| > 300`) |
| `host_id` | this crewd host |
| `checkouts[]` | this host's checkouts: `{toplevel, worktree_id, git_common_dir, case_insensitive, session_id, default_branch}`; one entry per (checkout, session), so two sessions in one checkout are two entries with the same `toplevel` |
| `settings` | the subset the gate needs (`SnapshotSettings`) |
| `bootstrap_zones` | true while no-zone bootstrap zones are in force (brief says "temporary zones") |
| `hmac` | `snapshot_hmac(key, snapshot)` = HMAC-SHA256 over `canonical_json` of every other key; tamper evidence only |

Written by crewd with an atomic rename. The gate never writes it. `toplevel` paths never leave the
host (they are not in any server payload).

## WebSocket (`/ws`)

Subscribe (`WsCrewSubscribe`):

```json
{"type":"subscribe","channel":"crew","crew_id":"crw_…","since_seq":1041,"topics":["crew"]}
{"type":"subscribe","channel":"crew","crew_id":"*","topics":["crew.summary"]}
```

Frames from the server (`WS_FRAMES`), following the existing `{"type": …}` framing of `/ws`:

| `type` | Shape | Notes |
|---|---|---|
| `crew.subscribed` | `{crew_id, since_seq, replayed}` | `replayed` ≤ 500 |
| `crew.event` | `{crew_id, data: EventEnvelope}` | replay first, then live; strictly by seq |
| `presence` | `{crew_id, lanes: [PresenceLane]}` | ephemeral, no seq, never replayed, ≤1 per session per 5 s |
| `resync_required` | `{crew_id, reason: gap_too_large\|overflow\|server_restart, last_seq}` | client refetches the snapshot |
| `crew.summary` | `{crews: [{crew_id, project_id, mode, live, moments, needs_you}]}` | counts only, filtered by `project_ids` |

`PresenceLane` = `{session_id, state, stuck, last_action: LastAction?, calls_since_checkpoint,
next_checkpoint_due_at?, limit?}`. `LastAction` = `{tool, path_rel?, verb?, age_s}`: command
metadata only, never a command string.

The reducer also accepts a client-local `{"type":"snapshot","data": CrewSnapshot}` frame (after a
REST refetch). `validate_ws_frame` checks any server frame, including the envelope inside
`crew.event`.

## Tree snapshot — `PUT /crews/{id}/tree`

Nested `{name, files, children[]}`: folder names and file counts only, at most 3 levels below
the root and 400 nodes (`validate_tree`). No file names, no contents.
