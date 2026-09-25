# The shared crew reducer

One reducer, two implementations: Python (`remembra.crew.reducer`, reference; used by crewd and
`remembra-crew watch`) and TypeScript (dashboard, WP-12). Both must pass every vector in
`tests/crew/vectors/reducer/`. Spec: §4.4.

## State (plain JSON)

```json
{
  "crew": CrewView | null,
  "mode": "solo" | "multi",
  "last_seq": 0,
  "needs_resync": false,
  "resync_reason": null | "gap" | "server",
  "sessions":  {"<id>": SessionView + {"presence": PresenceLane-without-session_id | null}},
  "claims":    {"<id>": ClaimView},          // live states only
  "zones":     {"<id>": ZoneView},           // not archived
  "tasks":     {"<id>": TaskView},           // all tasks, including done
  "collisions":{"<id>": CollisionView},      // open, acknowledged
  "decisions": {"<id>": DecisionView},       // proposed, in_force
  "offers":    {"<id>": OfferView},
  "inbox":     {"<id>": InboxItemView},      // live items seen since the snapshot
  "inbox_counts": {"project": 0, "crew": 0},
  "reports":   {"<task_id>": ReportView},    // current report per task
  "checkpoints": {"<session_id>": CheckpointView},  // latest per session
  "hosts":     {"<id>": {"id": "…", "state": "online|unreachable|retired"}},
  "pending_zone_changes": {"<change_id>": {"loosening": true | false | null}},
  "messages":  [MessageView],                // last 100 by seq
  "moments":   [{"seq", "type", "summary", "ts"}],   // last 50
  "batons":    [baton.passed payload + {"seq", "ts"}], // last 20
  "baton_refs": {"<ref>": {"seq", "task_id", "session_id", "dirty_files", "unpushed"}},  // last 50
  "guard_blocks":  {"<session_id>": count},
  "tamper_blocks": {"<session_id>": count},
  "budget": {"<metric>": {"used", "limit", "capped"}}
}
```

## Frame rules

1. `snapshot` → the state is rebuilt from the snapshot (`from_snapshot`): live claims, open
   collisions and live decisions only; every session gets `presence: null`; `last_seq =
   as_of_seq`; `needs_resync = false`.
2. Anything for another crew (`crew_id` differs) is ignored.
3. `resync_required` → `needs_resync = true`, `resync_reason = "server"`.
4. `crew.event`, while `needs_resync` is true → ignored.
5. `seq <= last_seq` → ignored (replay overlap or duplicate), even if the content differs.
6. `seq > last_seq + 1` → `needs_resync = true`, `resync_reason = "gap"`, not applied.
7. Otherwise apply by type (below), then `last_seq = seq` (also `crew.last_seq`), then, if the
   envelope says `moment`, append `{seq, type, summary, ts}` to `moments`.
8. An unknown type or `v != 1` only advances `last_seq` (an old client must not stall on a newer
   server).
9. `presence` → for each lane of a known session, replace that session's `presence` with the
   lane minus `session_id`. Never changes `state`, `stuck` or `last_seq`. Unknown sessions ignored.
10. `crew.subscribed`, `crew.summary` and unknown frames change nothing.

The session an event concerns is `refs.session_id`, else `actor.id` when `actor.kind = session`.

## Per-type application

| Types | Effect |
|---|---|
| `crew.created` | `crew = payload.crew`, `mode = crew.mode` |
| `crew.settings_changed` | `crew.settings_version`; `crew.enforcement` if present |
| `crew.mode_changed` | `mode = crew.mode = to` |
| `host.registered` / `unreachable` / `recovered` | `hosts[id].state` = view state / `unreachable` / `online` |
| `session.joined` | insert view with `presence: null` |
| `session.state_changed` | `state = to`; `quiet_reason` = payload value if `to = quiet`, else null; `state_reason = reason` |
| `session.recovered` | `state = active`, `quiet_reason = state_reason = null` |
| `session.quota_blocked` | `state = quota_blocked`, `state_reason = error` |
| `session.limit_warning` | `limit = {level, pct, source}` |
| `session.stuck` | `stuck` |
| `session.paused` / `resumed` | `state = paused` / `state = to` |
| `session.left` | `state = ended`, `end_reason = reason`, `ended_at = ts`, `presence = null` |
| `session.lost` | `state = lost`, `state_reason = reason`, `presence = null` |
| `activity.*` | `last_activity_at = ts`; `activity.commit` also sets `head_commit = sha` (unknown session: ignored) |
| `zone.created/updated/frozen/unfrozen` / `zone.archived` | upsert / remove |
| `zone.change_pending` / `change_decided` | add `{loosening}` / remove |
| `claim.*` with a `claim` view | upsert if live, else remove; if the claim is no longer `reserved`, remove its offers |
| `claim.offered_in_brief` | `offers[offer.id] = offer` |
| `claim.fenced` | `claims[claim_id].fenced = true` |
| `baton.passed` | append payload + seq/ts to `batons` |
| `baton.ref_created` | `baton_refs[ref] = {seq, task_id, session_id, dirty_files, unpushed}` (drop lowest seq over 50) |
| `guard.blocked` | `guard_blocks[session] += coalesced` |
| `guard.tamper_blocked` | `tamper_blocks[session] += 1` |
| `githook.missing` | `githook_state = payload.state` |
| `collision.*` | upsert if open/acknowledged, else remove |
| `task.*` | upsert `payload.task` |
| `checkpoint.created` | `checkpoints[session_id] = checkpoint` |
| `report.*` | current → `reports[task_id] = report`; not current → remove only if it is the stored current one |
| `message.posted` | append, sort by `seq`, keep last 100 |
| `message.edited` | replace by id if still held |
| `message.redacted` | `body = ""`, `body_truncated = false`, `redacted = true` |
| `decision.*` | upsert if proposed/in_force, else remove |
| `inbox.*` | live item: store it; `item_created` of an unseen project/crew item adds 1 to its count. Resolved/dismissed: remove it and subtract 1 (floor 0) from its audience count, whether or not it was seen (it was counted in the snapshot). Session-audience items are never counted. |
| `budget.warning` / `cap_reached` | `budget[metric] = {used, limit, capped}` |
| everything else | no state change (feed and moments only) |

## Vector format

```json
{"name": "…", "description": "…", "snapshot": CrewSnapshot, "frames": [frame, …],
 "expect": [assertion, …], "events_validate": false?}
```

Assertions address the final state by a path array (strings for object keys, integers for list
indices):

* `{"path": [...], "equals": value}` — deep equality, and the JSON type must match (`1` ≠ `true`);
* `{"path": [...], "absent": true}` — the path does not exist;
* `{"path": [...], "length": n}` — array length or object key count.

Compare after a JSON round trip. `events_validate: false` marks a vector that deliberately
contains frames outside the contract (unknown type, `v: 2`).
