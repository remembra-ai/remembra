# ruff: noqa: E501  (verbatim DDL from the spec)
"""crew.db v1 DDL exactly as written in the build spec (§3.2), for WP-14 tests.

WP-1 owns the real ``CREW_MIGRATIONS`` v1; these tests only need the tables that
``remembra.crew.access`` reads, built from the same contract text so the access
queries are checked against the agreed columns. ``test_access`` asserts that every
table in ``access.ENTITY_TABLES`` exists here with ``id`` and ``crew_id`` columns.
"""

CREW_DB_V1_DDL = """
-- Crew and membership
CREATE TABLE crews (
  id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, project_id TEXT NOT NULL, team_id TEXT,
  name TEXT, settings TEXT NOT NULL DEFAULT '{}', settings_version INTEGER NOT NULL DEFAULT 1,
  last_seq INTEGER NOT NULL DEFAULT 0, last_hash TEXT, active_zones_sha TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(owner_user_id, project_id));
CREATE TABLE crew_members (crew_id TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','admin','member','viewer')),
  added_by TEXT, added_at TEXT NOT NULL, PRIMARY KEY(crew_id, user_id));
CREATE TABLE project_shares (owner_user_id TEXT NOT NULL, project_id TEXT NOT NULL, team_id TEXT NOT NULL,
  shared_by TEXT, created_at TEXT NOT NULL, PRIMARY KEY(owner_user_id, project_id, team_id));   -- used from L1
CREATE INDEX idx_project_shares_team ON project_shares(team_id);

-- Hosts (one per crewd)
CREATE TABLE crew_hosts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, host_label TEXT NOT NULL,
  token_hash TEXT NOT NULL, platform TEXT, crewd_version TEXT, state TEXT NOT NULL DEFAULT 'online', -- online|unreachable|retired
  last_seen_at TEXT, registered_at TEXT NOT NULL);
CREATE INDEX idx_crew_hosts_user ON crew_hosts(user_id, state);

-- Sessions / presence
CREATE TABLE crew_sessions (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL, host_id TEXT, member_key TEXT NOT NULL, callsign TEXT NOT NULL,
  client_kind TEXT,                 -- hook|mcp|cli
  adapter TEXT, adapter_enforcement TEXT NOT NULL DEFAULT 'advisory',   -- enforced|advisory
  agent_verified INTEGER NOT NULL DEFAULT 0, model TEXT,
  checkout_fp TEXT, toplevel_rel TEXT, worktree_id TEXT, branch TEXT, head_commit TEXT,
  githook_state TEXT,               -- ok|missing|chained|unknown
  state TEXT NOT NULL, quiet_reason TEXT, stuck INTEGER NOT NULL DEFAULT 0, state_reason TEXT,
  joined_at TEXT NOT NULL, last_seen_at TEXT, last_activity_at TEXT, last_heartbeat_at TEXT,
  last_action TEXT,                 -- json {tool, path_rel, at}; command metadata only, never raw commands
  calls_since_checkpoint INTEGER NOT NULL DEFAULT 0, next_checkpoint_due_at TEXT, checkpoint_streak INTEGER NOT NULL DEFAULT 0,
  limit_level TEXT, limit_pct REAL, limit_source TEXT,
  current_task_id TEXT, last_checkpoint_id TEXT, delivered_seq INTEGER NOT NULL DEFAULT 0,
  token_hash TEXT NOT NULL, token_version INTEGER NOT NULL DEFAULT 1, agent_pid INTEGER,
  ended_at TEXT, end_reason TEXT,
  UNIQUE(crew_id, user_id, agent_id, session_id));
CREATE INDEX idx_crew_sessions_live ON crew_sessions(crew_id, state);
CREATE INDEX idx_crew_sessions_reap ON crew_sessions(state, last_heartbeat_at);
CREATE INDEX idx_crew_sessions_host ON crew_sessions(host_id, state);
CREATE UNIQUE INDEX uq_crew_callsign_live ON crew_sessions(crew_id, callsign) WHERE state NOT IN ('ended');

-- Zones
CREATE TABLE crew_zones (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, description TEXT,
  parent_id TEXT, is_leaf INTEGER NOT NULL DEFAULT 1, builtin INTEGER NOT NULL DEFAULT 0,
  include_globs TEXT NOT NULL, exclude_globs TEXT NOT NULL DEFAULT '[]',
  services TEXT NOT NULL DEFAULT '[]', command_patterns TEXT NOT NULL DEFAULT '[]',  -- argv-prefix grammar (D38)
  mcp_tools TEXT NOT NULL DEFAULT '[]',
  mode TEXT NOT NULL DEFAULT 'exclusive' CHECK(mode IN ('exclusive','shared','watch')),
  auto_claim INTEGER NOT NULL DEFAULT 1, protected INTEGER NOT NULL DEFAULT 0, reserve_for TEXT,
  fail_closed INTEGER NOT NULL DEFAULT 0, frozen_by TEXT, frozen_note TEXT, frozen_until TEXT,
  source TEXT NOT NULL CHECK(source IN ('repo','dashboard','api','suggested','builtin')), repo_sha TEXT,
  color TEXT, files_estimate INTEGER, version INTEGER NOT NULL DEFAULT 1,
  archived_at TEXT, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(crew_id, slug));
CREATE TABLE crew_zone_overlaps (crew_id TEXT, zone_a TEXT, zone_b TEXT, PRIMARY KEY(crew_id, zone_a, zone_b));
CREATE TABLE crew_zone_files (crew_id TEXT PRIMARY KEY, yaml_sha TEXT NOT NULL, branch TEXT, compiled TEXT NOT NULL,
  commons TEXT NOT NULL DEFAULT '[]', ignore TEXT NOT NULL DEFAULT '[]', enforcement TEXT, uploaded_by TEXT, uploaded_at TEXT);
CREATE TABLE crew_zone_changes (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, yaml_sha TEXT NOT NULL,
  uploaded_by_session TEXT, uploaded_by_user TEXT, diff TEXT NOT NULL, loosening INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','applied','rejected')), decided_by TEXT, decided_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE crew_repo_trees (crew_id TEXT PRIMARY KEY, tree TEXT NOT NULL, node_count INTEGER, captured_at TEXT);

-- Claims
CREATE TABLE crew_claims (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, zone_id TEXT, path_glob TEXT, resource TEXT,
  mode TEXT NOT NULL CHECK(mode IN ('exclusive','shared','watch')),
  holder_kind TEXT NOT NULL CHECK(holder_kind IN ('session','human')),
  holder_session_id TEXT, holder_user_id TEXT, holder_agent_id TEXT, task_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('requested','queued','active','offered','reserved','released','expired','revoked','denied')),
  source TEXT NOT NULL,             -- task|first_write|zones_file|dashboard|mcp|adopt|handover|micro_lease|local_arbiter
  epoch INTEGER NOT NULL DEFAULT 1, unconfirmed INTEGER NOT NULL DEFAULT 0, reason TEXT,
  lease_expires_at TEXT, reserve_reason TEXT, reserved_for TEXT, reserve_expires_at TEXT,
  offered_to TEXT, offer_expires_at TEXT, queue_pos INTEGER, baton_ref TEXT,
  granted_at TEXT, ended_at TEXT, end_reason TEXT, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX uq_claim_exclusive ON crew_claims(crew_id, zone_id)
  WHERE mode='exclusive' AND state IN ('active','offered','reserved') AND zone_id IS NOT NULL;
CREATE INDEX idx_claims_live   ON crew_claims(crew_id, state);
CREATE INDEX idx_claims_holder ON crew_claims(holder_session_id, state);
CREATE INDEX idx_claims_lease  ON crew_claims(state, lease_expires_at);
CREATE INDEX idx_claims_reserve ON crew_claims(state, reserve_expires_at);

-- Footprints / collisions
CREATE TABLE crew_footprints (crew_id TEXT, session_id TEXT, path TEXT, zone_ids TEXT, first_at TEXT, last_at TEXT,
  touches INTEGER DEFAULT 1, state TEXT NOT NULL DEFAULT 'dirty' CHECK(state IN ('dirty','committed','landed')),
  attribution TEXT NOT NULL DEFAULT 'certain' CHECK(attribution IN ('certain','probable')),
  claim_epoch INTEGER, last_commit TEXT, worktree_id TEXT, PRIMARY KEY(crew_id, session_id, path));
CREATE INDEX idx_footprints_path ON crew_footprints(crew_id, path, state);
CREATE TABLE crew_collisions (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  subject TEXT NOT NULL, zone_id TEXT, session_a TEXT, session_b TEXT, claim_id TEXT, attribution TEXT, evidence TEXT,
  state TEXT NOT NULL, detected_seq INTEGER, resolution TEXT, resolved_by TEXT, created_at TEXT, resolved_at TEXT);
CREATE UNIQUE INDEX uq_collision_open ON crew_collisions(crew_id, kind, subject, session_a, session_b)
  WHERE state IN ('open','acknowledged');

-- Tasks
CREATE TABLE crew_tasks (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL, body TEXT,
  status TEXT NOT NULL CHECK(status IN ('backlog','ready','claimed','in_progress','blocked','review','done','stalled','cancelled')),
  status_before_stall TEXT, phase TEXT, position REAL, priority INTEGER DEFAULT 2, zone_ids TEXT NOT NULL DEFAULT '[]',
  claim_mode TEXT DEFAULT 'exclusive',
  owner_session_id TEXT, owner_user_id TEXT, owner_agent_id TEXT, assignee_hint TEXT, reviewer TEXT, parent_id TEXT,
  labels TEXT DEFAULT '[]', acceptance TEXT NOT NULL DEFAULT '[]', acceptance_locked INTEGER NOT NULL DEFAULT 0,
  started_head TEXT, started_at TEXT, done_at TEXT, current_report_id TEXT, blocked_reason TEXT, stalled_at TEXT,
  created_by TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(crew_id, number));
CREATE INDEX idx_tasks_status ON crew_tasks(crew_id, status);
CREATE INDEX idx_tasks_owner  ON crew_tasks(owner_session_id);
CREATE TABLE crew_task_deps (crew_id TEXT NOT NULL, task_id TEXT, depends_on_id TEXT, PRIMARY KEY(crew_id, task_id, depends_on_id));

-- Checkpoints / reports / batons
CREATE TABLE crew_checkpoints (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT,
  trigger TEXT NOT NULL, facts TEXT, facts_hash TEXT NOT NULL, headline TEXT, facts_source TEXT NOT NULL,
  compacted INTEGER NOT NULL DEFAULT 0, memory_id TEXT, seq INTEGER, created_at TEXT NOT NULL, UNIQUE(session_id, facts_hash));
CREATE INDEX idx_ckp_session ON crew_checkpoints(crew_id, session_id, created_at);
CREATE INDEX idx_ckp_task    ON crew_checkpoints(task_id, created_at);
CREATE TABLE crew_reports (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('completion','partial','stalled','waived')), verdict TEXT,
  criteria TEXT, commits TEXT, files TEXT, out_of_zone_files TEXT, tests TEXT, deploy TEXT,
  sections TEXT, summary TEXT, grounding TEXT, facts_source TEXT NOT NULL, facts_hash TEXT,
  review_state TEXT, reviewed_by TEXT, review_note TEXT, is_current INTEGER NOT NULL DEFAULT 1, superseded_reason TEXT,
  handoff_id TEXT, baton_ref TEXT, memory_id TEXT, created_at TEXT NOT NULL, UNIQUE(task_id, session_id, facts_hash));
CREATE UNIQUE INDEX uq_report_current ON crew_reports(task_id) WHERE is_current=1;
CREATE TABLE crew_batons (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, task_id TEXT, from_session TEXT, to_session TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('adopt','handover','same_checkout','human_assign','reserved_for','first_write')),
  offer_id TEXT, handoff_id TEXT, checkpoint_id TEXT, report_id TEXT, baton_ref TEXT, restored INTEGER,
  zone_ids TEXT, brief_text TEXT, seq INTEGER, created_at TEXT NOT NULL);
CREATE INDEX idx_batons_task ON crew_batons(task_id, created_at);
CREATE TABLE crew_baton_offers (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, claim_id TEXT NOT NULL, task_id TEXT,
  to_session TEXT NOT NULL, via TEXT NOT NULL CHECK(via IN ('brief','human','reserved_for')), created_at TEXT NOT NULL,
  used_at TEXT, UNIQUE(claim_id, to_session));

-- Channel / decisions (proposals and votes: L1, tables created now)
CREATE TABLE crew_messages (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, seq INTEGER, thread_root_id TEXT, reply_to_id TEXT,
  kind TEXT NOT NULL, author_kind TEXT NOT NULL, author_user_id TEXT, author_session_id TEXT, author_agent_id TEXT,
  author_verified INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL, mentions TEXT, refs TEXT, client_msg_id TEXT, trust_score REAL, pinned INTEGER DEFAULT 0,
  edited_at TEXT, redacted INTEGER DEFAULT 0, redacted_body_hash TEXT, created_at TEXT NOT NULL,
  UNIQUE(crew_id, author_user_id, client_msg_id));
CREATE INDEX idx_msgs_seq    ON crew_messages(crew_id, seq);
CREATE INDEX idx_msgs_thread ON crew_messages(thread_root_id, seq);
CREATE TABLE crew_message_edits (message_id TEXT NOT NULL, crew_id TEXT NOT NULL, prev_body TEXT NOT NULL, edited_at TEXT NOT NULL);
CREATE TABLE crew_proposals (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, message_id TEXT,
  title TEXT, body TEXT, options TEXT NOT NULL, quorum TEXT NOT NULL, eligible TEXT NOT NULL, deadline_at TEXT,
  state TEXT NOT NULL, outcome TEXT, decided_by TEXT, decision_id TEXT, created_at TEXT, closed_at TEXT,
  UNIQUE(crew_id, number));
CREATE TABLE crew_votes (crew_id TEXT NOT NULL, proposal_id TEXT, voter_kind TEXT, voter_id TEXT, voter_verified INTEGER NOT NULL DEFAULT 0,
  choice TEXT, blocking INTEGER DEFAULT 0, rationale TEXT, created_at TEXT, PRIMARY KEY(proposal_id, voter_kind, voter_id));
CREATE TABLE crew_decisions (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT, decision TEXT,
  rationale TEXT, alternatives TEXT, decided_by_kind TEXT, decided_by TEXT, participants TEXT,
  source TEXT CHECK(source IN ('direct','proposal','override')), task_id TEXT, zone_id TEXT, supersedes_id TEXT,
  state TEXT NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','in_force','rejected','superseded')),
  confirmed_by TEXT, confirmed_at TEXT, memory_id TEXT, created_at TEXT, UNIQUE(crew_id, number));

-- Inboxes / cursors
CREATE TABLE crew_inbox_items (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL,
  audience TEXT NOT NULL CHECK(audience IN ('project','crew','session')), recipient TEXT,
  kind TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'server' CHECK(origin IN ('server','human','agent')),
  ref_type TEXT, ref_id TEXT, priority INTEGER NOT NULL DEFAULT 2, title TEXT NOT NULL,
  primary_action TEXT, state TEXT NOT NULL CHECK(state IN ('open','seen','claimed','resolved','dismissed')),
  claimed_by TEXT, dedupe_key TEXT NOT NULL, coalesced_count INTEGER NOT NULL DEFAULT 1,
  created_seq INTEGER, resolved_seq INTEGER, resolved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX uq_inbox_open ON crew_inbox_items(crew_id, dedupe_key) WHERE state IN ('open','seen','claimed');
CREATE INDEX idx_inbox_aud ON crew_inbox_items(crew_id, audience, recipient, state);
CREATE TABLE crew_read_cursors (crew_id TEXT, principal TEXT, stream TEXT, last_seq INTEGER, updated_at TEXT,
  PRIMARY KEY(crew_id, principal, stream));

-- Human bypass codes (D34)
CREATE TABLE crew_bypass_codes (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, code_hash TEXT NOT NULL, session_id TEXT,
  scope TEXT NOT NULL, issued_by TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL);

-- Event log (the truth), hash-chained
CREATE TABLE crew_events (crew_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
  project_id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, v INTEGER NOT NULL DEFAULT 1,
  actor_kind TEXT, actor_id TEXT, session_id TEXT, task_id TEXT, zone_id TEXT, ref_id TEXT,
  severity TEXT NOT NULL DEFAULT 'info', moment INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL,
  payload TEXT NOT NULL, idem_key TEXT, origin TEXT NOT NULL DEFAULT 'server', prev_hash TEXT, hash TEXT,
  UNIQUE(crew_id, seq));
CREATE UNIQUE INDEX uq_events_idem ON crew_events(crew_id, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX idx_events_moment ON crew_events(crew_id, moment, seq);
CREATE INDEX idx_events_task   ON crew_events(task_id, seq);
CREATE INDEX idx_events_sess   ON crew_events(session_id, seq);
-- idx_events_type and idx_events_ts are added only if WP-15 load tests show a query needs them.
CREATE TABLE crew_digests (crew_id TEXT, day TEXT, counts TEXT, moments TEXT, PRIMARY KEY(crew_id, day));

-- Cross-DB outbox (memory promotions, relay handoffs) and idempotency
CREATE TABLE crew_outbox (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, result_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE crew_idempotency (key TEXT PRIMARY KEY, principal TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL);   -- never stores token-bearing responses; 72 h retention

-- Notifications
CREATE TABLE crew_notification_rules (user_id TEXT, crew_id TEXT, kind TEXT, channel TEXT, quiet_hours TEXT,
  batch_window_s INTEGER DEFAULT 120, PRIMARY KEY(user_id, crew_id, kind));
CREATE TABLE crew_notify_targets (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('email','webhook')),
  target TEXT NOT NULL, secret_hash TEXT, verified_at TEXT, created_at TEXT NOT NULL);   -- human-configured only
"""
