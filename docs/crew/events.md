# Crew events: envelope, closed set, moments, hash chain

Source of truth: `remembra.crew.schemas` (`ENVELOPE`, `EVENT_SPECS`, `is_moment`, `event_hash`).
Vectors: `tests/crew/vectors/events/samples.json` (one valid envelope per L0 type, plus invalid envelopes and client events).
Spec: §4.1–§4.3.

## Envelope

| Field | Rule |
|---|---|
| `seq` | per-crew, gap-free, starts at 1, assigned inside `BEGIN IMMEDIATE` |
| `id` | `evt_…` |
| `crew_id`, `project_id` | `crw_…`; project id as stored |
| `ts` | server receipt time, ISO-8601 UTC with `Z` |
| `type`, `v` | a member of the closed set below; `v` = 1 for every L0 type |
| `origin` | `server` or `client` (client only for the whitelisted types) |
| `actor` | `{kind: session\|human\|system, id, callsign?, agent_id?, user_id?, verified}`, **derived from the credential**, never from the body |
| `refs` | optional ids: `zone_id, task_id, claim_id, session_id, report_id, collision_id, message_id, decision_id, inbox_item_id, host_id` |
| `severity` | `info \| notice \| low \| medium \| high \| critical` |
| `moment` | set by the server from `is_moment` (`validate_envelope` rejects a wrong flag) |
| `summary` | ≤300 chars, server template, ids/slugs/callsigns only |
| `payload` | ≤8 KB canonical JSON, validated against the type's payload shape (closed objects: unknown keys are errors) |

Every object in the contract is **closed**: an unknown key is a validation error. That is how "never
tokens, never raw commands" is enforced structurally (a `token` in a session view or a `command` in
`last_action` fails validation).

Paths that leave the host are `PATH_REL`: repo-relative POSIX, no leading `/`, no `~`, no `..`
segment, no backslash, no control characters.

## Entity views

Events that change an entity carry the **whole post-change view** of it (`session`, `claim`,
`zone`, `task`, `collision`, `decision`, `report`, `item`, `message`, `checkpoint`,
`offer`). Reducers upsert by id; they never need a second lookup. Views: `CrewView`,
`SessionView`, `ClaimView`, `ZoneView`, `TaskView` (with `Criterion`), `CollisionView`,
`DecisionView`, `MessageView` (body clipped to 4,000 chars with `body_truncated`),
`InboxItemView`, `ReportView` (with `CriterionResult`), `CheckpointView`, `HostView`,
`OfferView`, `FootprintView`, `Blocker`, `LimitView`, `LastAction`. Field lists:
`docs/crew/openapi.json` → `components.schemas`.

## Closed event set

`?` = optional or nullable. "if human" = a moment when the actor is a human (every human-only
action is a moment); client-submittable types never count as human actions.

| Type | Payload keys | Client | Moment | Release |
|---|---|---|---|---|
| `crew.created` | `crew` |  | if human | L0 |
| `crew.settings_changed` | `settings_version`, `changed_keys`, `enforcement`? |  | if human | L0 |
| `crew.mode_changed` | `from`, `to`, `live_sessions` |  | if to = multi | L0 |
| `crew.shift_started` | `shift_id`, `live_sessions` |  | if human | L0 |
| `crew.shift_ended` | `shift_id`, `duration_s` |  | if human | L0 |
| `host.registered` | `host` |  | if human | L0 |
| `host.unreachable` | `host_id`, `silent_s`, `session_ids` |  | always | L0 |
| `host.recovered` | `host_id`, `down_s` |  | if human | L0 |
| `session.joined` | `session`, `resume_of`?, `observe_only` |  | if human | L0 |
| `session.state_changed` | `from`, `to`, `reason`, `quiet_reason`? |  | if human | L0 |
| `session.recovered` | `from`, `down_s`, `claims_retaken`, `tasks_restored`, `superseded_report_ids` |  | always | L0 |
| `session.quota_blocked` | `error`, `source`, `baton_ref`?, `claims_reserved` |  | always | L0 |
| `session.limit_warning` | `level`, `pct`?, `source` |  | if human | L0 |
| `session.stuck` | `signal`, `stuck` |  | if human | L0 |
| `session.paused` | `reason` |  | if human | L0 |
| `session.resumed` | `to`, `reason`? |  | if human | L0 |
| `session.left` | `reason`, `claims_released`, `claims_reserved` |  | if human | L0 |
| `session.lost` | `reason`, `last_signal_age_s` |  | always | L0 |
| `session.token_rotated` | `token_version` |  | if human | L0 |
| `activity.burst` | `files_touched`, `command_verbs`, `tests` | yes | no | L0 |
| `activity.commit` | `sha`, `subject_hash`, `files`, `branch`? | yes | no | L0 |
| `activity.push` | `upstream`, `count`, `default_branch`, `head`? | yes | if default_branch | L0 |
| `activity.deploy` | `target`, `status` | yes | always | L0 |
| `activity.test_verdict_changed` | `fingerprint`, `from`, `to`, `passed`, `failed` | yes | no | L0 |
| `zone.created` | `zone` |  | if human | L0 |
| `zone.updated` | `zone` |  | if human | L0 |
| `zone.archived` | `zone` |  | if human | L0 |
| `zone.frozen` | `zone`, `reason` |  | if human | L0 |
| `zone.unfrozen` | `zone`, `reason` |  | if human | L0 |
| `zone.synced` | `sha`, `branch`?, `diff_summary`, `policy_changed`, `zone_ids` |  | if policy_changed | L0 |
| `zone.change_pending` | `change_id`, `sha`, `loosening`, `diff_summary` |  | always | L0 |
| `zone.change_decided` | `change_id`, `decision` |  | if human | L0 |
| `zone.suggested_applied` | `zone_ids`, `undo_available` |  | if human | L0 |
| `claim.requested` | `claim` |  | if human | L0 |
| `claim.granted` | `claim` |  | if human | L0 |
| `claim.queued` | `claim` |  | if human | L0 |
| `claim.denied` | `claim`?, `blockers` |  | if human | L0 |
| `claim.released` | `claim`, `baton` |  | if human | L0 |
| `claim.expired` | `claim` |  | if human | L0 |
| `claim.reserved` | `claim`, `reason` |  | if human | L0 |
| `claim.adopted` | `claim`, `cross_checkout`, `from_session`? |  | if cross_checkout | L0 |
| `claim.offered_in_brief` | `offer` |  | if human | L0 |
| `claim.handover_offered` | `claim`, `to_session` |  | if human | L0 |
| `claim.handover_accepted` | `claim` |  | if human | L0 |
| `claim.handover_declined` | `claim`, `reason` |  | if human | L0 |
| `claim.revoked` | `claim`, `reason` |  | if human | L0 |
| `claim.transferred` | `claim`, `from_session`?, `reason` |  | if human | L0 |
| `claim.fenced` | `claim_id`, `horizon_at` |  | if human | L0 |
| `claim.unconfirmed` | `claim` |  | if human | L0 |
| `baton.passed` | `baton_id`, `task_id`?, `from_session`?, `to_session`, `kind`, `handoff_id`?, `zones`, `baton_ref`?, `restored`? |  | always | L0 |
| `baton.restored` | `baton_id`, `task_id`?, `to_session`, `baton_ref`?, `restored`, `status`, `files` |  | if not restored | L0 |
| `baton.ref_created` | `ref`, `task_id`?, `dirty_files`, `unpushed`, `skipped_files`? |  | if human | L0 |
| `guard.blocked` | `path_rel`?, `zone`?, `holder`?, `rule`, `op`, `decision`, `surface`, `coalesced` | yes | no | L0 |
| `guard.bypass_used` | `code_id`, `scope` |  | always | L0 |
| `guard.tamper_blocked` | `kind`, `surface` | yes | always | L0 |
| `gate.error` | `stage`, `error_class` | yes | no | L0 |
| `gate.deadline` | `stage`, `elapsed_ms`, `unconfirmed_zone`? | yes | no | L0 |
| `gate.tampered` | `expected_sha`, `actual_sha`, `restored` |  | always | L0 |
| `githook.missing` | `hook`, `state`, `worktree_id`? | yes | no | L0 |
| `collision.detected` | `collision` |  | if severity high/critical | L0 |
| `collision.acknowledged` | `collision` |  | if human | L0 |
| `collision.resolved` | `collision` |  | if human | L0 |
| `collision.dismissed` | `collision` |  | if human | L0 |
| `collision.escalated` | `collision` |  | if human | L0 |
| `task.created` | `task` |  | if human | L0 |
| `task.updated` | `task`, `changed` |  | if human | L0 |
| `task.status_changed` | `task`, `from`, `to` |  | if human | L0 |
| `task.assigned` | `task`, `to_session` |  | if human | L0 |
| `task.stalled` | `task`, `reason` |  | if human | L0 |
| `task.recovered` | `task` |  | if human | L0 |
| `task.review_requested` | `task`, `report_id` |  | if human | L0 |
| `task.review_decided` | `task`, `report_id`, `decision` |  | if human | L0 |
| `task.done` | `task`, `report_id` |  | always | L0 |
| `task.reopened` | `task` |  | if human | L0 |
| `task.deps_changed` | `task` |  | if human | L0 |
| `task.acceptance_changed` | `task`, `criteria_count` |  | if human | L0 |
| `checkpoint.created` | `checkpoint` |  | if human | L0 |
| `checkpoint.missed` | `overdue_s`, `nudge` |  | if human | L0 |
| `report.submitted` | `report` |  | if human | L0 |
| `report.accepted` | `report` |  | if human | L0 |
| `report.rejected` | `report` |  | always | L0 |
| `report.waived` | `report` |  | if human | L0 |
| `report.superseded` | `report` |  | if human | L0 |
| `handoff.created` | `handoff_id`, `end_reason`, `facts_source`, `task_id`? |  | if human | L0 |
| `message.posted` | `message` |  | if human | L0 |
| `message.edited` | `message`, `after_delivery` |  | if human | L0 |
| `message.redacted` | `message_id` |  | if human | L0 |
| `decision.proposed` | `decision` |  | if human | L0 |
| `decision.confirmed` | `decision` |  | always | L0 |
| `decision.rejected` | `decision` |  | if human | L0 |
| `decision.superseded` | `decision` |  | if human | L0 |
| `proposal.opened` | `proposal_id` |  | if human | L1 |
| `proposal.resolved` | `proposal_id`, `outcome` |  | if human | L1 |
| `vote.cast` | `proposal_id`, `choice`, `verified` |  | if human | L1 |
| `objection.raised` | `proposal_id` |  | if human | L1 |
| `failure.recorded` | `failure_id`, `session_id`, `kind` |  | if human | L1 (reserved, no producer) |
| `failure.resolved` | `failure_id`, `resolution` |  | if human | L1 (reserved, no producer) |
| `artifact.recorded` | `artifact_id`, `kind`, `content_hash` |  | if human | L1 (reserved, no producer) |
| `inbox.item_created` | `item` |  | if human | L0 |
| `inbox.item_claimed` | `item` |  | if human | L0 |
| `inbox.item_resolved` | `item` |  | if human | L0 |
| `human.override` | `action`, `reason`, `target_kind`, `target_id` |  | always | L0 |
| `budget.warning` | `metric`, `used`, `limit` |  | if human | L0 |
| `budget.cap_reached` | `metric`, `used`, `limit` |  | if human | L0 |

L1 types are named so the set is closed, but an L0 server must not emit them
(`validate_event_payload` rejects them unless `allow_l1=True`).

## Client-submittable whitelist

`activity.burst`, `activity.commit`, `activity.push`, `activity.deploy`,
`activity.test_verdict_changed`, `guard.blocked`, `guard.tamper_blocked`, `gate.error`,
`gate.deadline`, `githook.missing`. Everything else is server-emitted only.

A client item (`POST /crews/{id}/events`, ≤50 per call, or inside a heartbeat) is
`{id, type, age_s?, payload}` (`ClientEvent`): no actor, no session id, no timestamp. The server
derives actor and session from the session token and stamps `ts` itself (`age_s` lets it
backdate by a client-reported age, never an absolute time). `validate_client_event` is the check.

## Idempotency

* Client keys are stored as `c:<client event id>` (`client_idem_key`). Server keys must never
  start with `c:` (`server_idem_key` raises), so a client can never suppress a server event.
* Natural keys: join `(crew, user, agent, session_id)`, heartbeat `(host, batch_id)`, checkpoint
  `(session, facts_hash)`, report `(task, session, facts_hash)`, message `client_msg_id`.
* A repeated join never returns the existing token (§4.3). Token-bearing responses are never stored.

## Hash chain

`hash = sha256(prev_hash ‖ canonical_json(event without hash/prev_hash))`, hex. The first event of a
crew uses `prev_hash = GENESIS_HASH` (64 zeros). `canonical_json` = sorted keys, no whitespace,
UTF-8, NaN rejected. `verify_chain(events)` checks seq continuity, `prev_hash` links and each hash
(the nightly verify job).

Retention deletes old non-moment events, so the stored chain has gaps. Every deleted run is
recorded in `crew_pruned_ranges` (`first_seq, last_seq, prev_hash` of the first deleted event,
`last_hash` of the last one) in the same transaction as the delete, adjacent runs merged. The server
verifier (`remembra.crew.events.verify_crew_chain`) accepts a gap, including one at the tail below
`crews.last_seq`, only when it equals a recorded range that links to the events on both sides; it
also reports ranges that match no gap and moments listed in `crew_digests` that are missing. The
chain is plain SHA-256, so it detects partial edits and deletions of the SQLite file, not a full
re-forge by someone who rewrites every hash.

## Moment rules

Always: `task.done`, `baton.passed`, `session.lost`, `session.recovered`,
`session.quota_blocked`, `host.unreachable`, `decision.confirmed`, `activity.deploy`,
`report.rejected`, `human.override`, `guard.bypass_used`, `guard.tamper_blocked`,
`gate.tampered`, `zone.change_pending`. Conditional: `collision.detected` with severity high or
critical, `activity.push` to the default branch, `claim.adopted` with `cross_checkout`,
`zone.synced` with `policy_changed`, `crew.mode_changed` to `multi`. Plus every server event
whose actor is a human.

Collision severities: `same_worktree_file` critical, `foreign_checkout_write` critical,
`exclusive_breach` high, `stale_epoch_write` high, `same_file` medium, `merge_conflict_risk`
medium, `same_zone_shared` low, `unattributed_change` notice.
