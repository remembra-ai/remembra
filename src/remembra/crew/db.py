"""``crew.db``: the separate SQLite file that holds every Crew-mode table (spec D35, §3.2).

Why a second file: the main database has one aiosqlite connection per process
and every transaction takes one process-wide ``asyncio.Lock``
(``storage/sqlite_tx.py``), so crew claims and heartbeats would queue behind
every tenant's memory writes. ``crew.db`` has its own connection, its own
:class:`~remembra.storage.sqlite_tx.TxCoordinator` (its own lock), WAL mode and
a 5 s busy timeout, and its own versioned migration list,
:data:`CREW_MIGRATIONS`, applied by the same
:class:`~remembra.storage.database.MigrationRunner` as the main database.

Cross-database effects (memory promotions, relay handoffs) never write the main
database from inside a crew transaction: they are queued in ``crew_outbox`` and
applied by :class:`remembra.crew.outbox.CrewOutboxWorker`.

Every mutation must run in :meth:`CrewDatabase.transaction` (``BEGIN IMMEDIATE``)
and stay a handful of statements; never await network I/O inside it.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from remembra.storage.database import Migration, MigrationRunner
from remembra.storage.sqlite_tx import AfterCommit, GuardedConnection, TxCoordinator

log = structlog.get_logger(__name__)

CREW_DB_FILENAME = "crew.db"
CREW_DB_PATH_ENV = "REMEMBRA_CREW_DB_PATH"
CREW_BUSY_TIMEOUT_MS = 5000

# ---------------------------------------------------------------------------
# CREW_MIGRATIONS v1 (spec §3.2). Append only: never edit an applied entry.
#
# v1 was amended once, before crew.db first deployed (continuity gap analysis §7,
# "L0 riders"): nullable columns that change no behaviour when NULL, on
# crew_sessions (provider, parent_session_id, sub_agent_id, run_id, capabilities,
# context_window, env_fp_id), crew_checkpoints (run_id, state_before_ref,
# decision_ids, quality, confidence, continuity_seq), crew_decisions (evidence,
# proposed_by_verified, decided_by_verified, intent_version) and crew_footprints
# (content_hash, artifact_id). A sub-agent is its own session row linked to the
# session that started it by parent_session_id (owner decision, open question 1).
# Also before the first deploy: crew_event_tombstones, so account erasure can remove
# an erased account's content from other owners' hash-chained logs (R-23).
# From the first deploy on, any change is a new version.
# ---------------------------------------------------------------------------

_V1_CREW_AND_MEMBERSHIP = [
    """CREATE TABLE crews (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, project_id TEXT NOT NULL, team_id TEXT,
      name TEXT, settings TEXT NOT NULL DEFAULT '{}', settings_version INTEGER NOT NULL DEFAULT 1,
      last_seq INTEGER NOT NULL DEFAULT 0, last_hash TEXT, active_zones_sha TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(owner_user_id, project_id))""",
    """CREATE TABLE crew_members (crew_id TEXT NOT NULL, user_id TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('owner','admin','member','viewer')),
      added_by TEXT, added_at TEXT NOT NULL, PRIMARY KEY(crew_id, user_id))""",
    # Needed by "list the crews this user belongs to" (WP-8 GET /crews, WS crew.summary).
    "CREATE INDEX idx_crew_members_user ON crew_members(user_id)",
    """CREATE TABLE project_shares (owner_user_id TEXT NOT NULL, project_id TEXT NOT NULL, team_id TEXT NOT NULL,
      shared_by TEXT, created_at TEXT NOT NULL, PRIMARY KEY(owner_user_id, project_id, team_id))""",
    "CREATE INDEX idx_project_shares_team ON project_shares(team_id)",
]

_V1_HOSTS_AND_SESSIONS = [
    """CREATE TABLE crew_hosts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, host_label TEXT NOT NULL,
      token_hash TEXT NOT NULL, platform TEXT, crewd_version TEXT, state TEXT NOT NULL DEFAULT 'online',
      last_seen_at TEXT, registered_at TEXT NOT NULL)""",
    "CREATE INDEX idx_crew_hosts_user ON crew_hosts(user_id, state)",
    """CREATE TABLE crew_sessions (
      id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
      session_id TEXT NOT NULL, host_id TEXT, member_key TEXT NOT NULL, callsign TEXT NOT NULL,
      client_kind TEXT,
      adapter TEXT, adapter_enforcement TEXT NOT NULL DEFAULT 'advisory',
      agent_verified INTEGER NOT NULL DEFAULT 0, model TEXT,
      checkout_fp TEXT, toplevel_rel TEXT, worktree_id TEXT, branch TEXT, head_commit TEXT,
      githook_state TEXT,
      state TEXT NOT NULL, quiet_reason TEXT, stuck INTEGER NOT NULL DEFAULT 0, state_reason TEXT,
      joined_at TEXT NOT NULL, last_seen_at TEXT, last_activity_at TEXT, last_heartbeat_at TEXT,
      last_action TEXT,
      calls_since_checkpoint INTEGER NOT NULL DEFAULT 0, next_checkpoint_due_at TEXT,
      checkpoint_streak INTEGER NOT NULL DEFAULT 0,
      limit_level TEXT, limit_pct REAL, limit_source TEXT,
      current_task_id TEXT, last_checkpoint_id TEXT, delivered_seq INTEGER NOT NULL DEFAULT 0,
      token_hash TEXT NOT NULL, token_version INTEGER NOT NULL DEFAULT 1, agent_pid INTEGER,
      ended_at TEXT, end_reason TEXT,
      provider TEXT, parent_session_id TEXT, sub_agent_id TEXT, run_id TEXT, capabilities TEXT, context_window INTEGER,
      env_fp_id TEXT,
      UNIQUE(crew_id, user_id, agent_id, session_id))""",
    "CREATE INDEX idx_crew_sessions_live ON crew_sessions(crew_id, state)",
    "CREATE INDEX idx_crew_sessions_reap ON crew_sessions(state, last_heartbeat_at)",
    "CREATE INDEX idx_crew_sessions_host ON crew_sessions(host_id, state)",
    "CREATE UNIQUE INDEX uq_crew_callsign_live ON crew_sessions(crew_id, callsign) WHERE state NOT IN ('ended')",
    "CREATE INDEX idx_crew_sessions_parent ON crew_sessions(parent_session_id) WHERE parent_session_id IS NOT NULL",
]

_V1_ZONES = [
    """CREATE TABLE crew_zones (
      id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, slug TEXT NOT NULL, title TEXT NOT NULL, description TEXT,
      parent_id TEXT, is_leaf INTEGER NOT NULL DEFAULT 1, builtin INTEGER NOT NULL DEFAULT 0,
      include_globs TEXT NOT NULL, exclude_globs TEXT NOT NULL DEFAULT '[]',
      services TEXT NOT NULL DEFAULT '[]', command_patterns TEXT NOT NULL DEFAULT '[]',
      mcp_tools TEXT NOT NULL DEFAULT '[]',
      mode TEXT NOT NULL DEFAULT 'exclusive' CHECK(mode IN ('exclusive','shared','watch')),
      auto_claim INTEGER NOT NULL DEFAULT 1, protected INTEGER NOT NULL DEFAULT 0, reserve_for TEXT,
      fail_closed INTEGER NOT NULL DEFAULT 0, frozen_by TEXT, frozen_note TEXT, frozen_until TEXT,
      source TEXT NOT NULL CHECK(source IN ('repo','dashboard','api','suggested','builtin')), repo_sha TEXT,
      color TEXT, files_estimate INTEGER, version INTEGER NOT NULL DEFAULT 1,
      archived_at TEXT, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(crew_id, slug))""",
    "CREATE TABLE crew_zone_overlaps (crew_id TEXT, zone_a TEXT, zone_b TEXT, PRIMARY KEY(crew_id, zone_a, zone_b))",
    """CREATE TABLE crew_zone_files (crew_id TEXT PRIMARY KEY, yaml_sha TEXT NOT NULL, branch TEXT, compiled TEXT NOT NULL,
      commons TEXT NOT NULL DEFAULT '[]', ignore TEXT NOT NULL DEFAULT '[]', enforcement TEXT, uploaded_by TEXT,
      uploaded_at TEXT)""",
    """CREATE TABLE crew_zone_changes (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, yaml_sha TEXT NOT NULL,
      uploaded_by_session TEXT, uploaded_by_user TEXT, diff TEXT NOT NULL, loosening INTEGER NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','applied','rejected')), decided_by TEXT, decided_at TEXT,
      created_at TEXT NOT NULL)""",
    "CREATE TABLE crew_repo_trees (crew_id TEXT PRIMARY KEY, tree TEXT NOT NULL, node_count INTEGER, captured_at TEXT)",
]

_V1_CLAIMS_FOOTPRINTS_COLLISIONS = [
    """CREATE TABLE crew_claims (
      id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, zone_id TEXT, path_glob TEXT, resource TEXT,
      mode TEXT NOT NULL CHECK(mode IN ('exclusive','shared','watch')),
      holder_kind TEXT NOT NULL CHECK(holder_kind IN ('session','human')),
      holder_session_id TEXT, holder_user_id TEXT, holder_agent_id TEXT, task_id TEXT,
      state TEXT NOT NULL CHECK(state IN
        ('requested','queued','active','offered','reserved','released','expired','revoked','denied')),
      source TEXT NOT NULL,
      epoch INTEGER NOT NULL DEFAULT 1, unconfirmed INTEGER NOT NULL DEFAULT 0, reason TEXT,
      lease_expires_at TEXT, reserve_reason TEXT, reserved_for TEXT, reserve_expires_at TEXT,
      offered_to TEXT, offer_expires_at TEXT, queue_pos INTEGER, baton_ref TEXT,
      granted_at TEXT, ended_at TEXT, end_reason TEXT, version INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX uq_claim_exclusive ON crew_claims(crew_id, zone_id)
      WHERE mode='exclusive' AND state IN ('active','offered','reserved') AND zone_id IS NOT NULL""",
    "CREATE INDEX idx_claims_live ON crew_claims(crew_id, state)",
    "CREATE INDEX idx_claims_holder ON crew_claims(holder_session_id, state)",
    "CREATE INDEX idx_claims_lease ON crew_claims(state, lease_expires_at)",
    "CREATE INDEX idx_claims_reserve ON crew_claims(state, reserve_expires_at)",
    """CREATE TABLE crew_footprints (crew_id TEXT, session_id TEXT, path TEXT, zone_ids TEXT, first_at TEXT, last_at TEXT,
      touches INTEGER DEFAULT 1, state TEXT NOT NULL DEFAULT 'dirty' CHECK(state IN ('dirty','committed','landed')),
      attribution TEXT NOT NULL DEFAULT 'certain' CHECK(attribution IN ('certain','probable')),
      claim_epoch INTEGER, last_commit TEXT, worktree_id TEXT, content_hash TEXT, artifact_id TEXT,
      PRIMARY KEY(crew_id, session_id, path))""",
    "CREATE INDEX idx_footprints_path ON crew_footprints(crew_id, path, state)",
    """CREATE TABLE crew_collisions (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
      subject TEXT NOT NULL, zone_id TEXT, session_a TEXT, session_b TEXT, claim_id TEXT, attribution TEXT, evidence TEXT,
      state TEXT NOT NULL, detected_seq INTEGER, resolution TEXT, resolved_by TEXT, created_at TEXT, resolved_at TEXT)""",
    """CREATE UNIQUE INDEX uq_collision_open ON crew_collisions(crew_id, kind, subject, session_a, session_b)
      WHERE state IN ('open','acknowledged')""",
]

_V1_TASKS_REPORTS_BATONS = [
    """CREATE TABLE crew_tasks (
      id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL, body TEXT,
      status TEXT NOT NULL CHECK(status IN
        ('backlog','ready','claimed','in_progress','blocked','review','done','stalled','cancelled')),
      status_before_stall TEXT, phase TEXT, position REAL, priority INTEGER DEFAULT 2,
      zone_ids TEXT NOT NULL DEFAULT '[]',
      claim_mode TEXT DEFAULT 'exclusive',
      owner_session_id TEXT, owner_user_id TEXT, owner_agent_id TEXT, assignee_hint TEXT, reviewer TEXT, parent_id TEXT,
      labels TEXT DEFAULT '[]', acceptance TEXT NOT NULL DEFAULT '[]', acceptance_locked INTEGER NOT NULL DEFAULT 0,
      started_head TEXT, started_at TEXT, done_at TEXT, current_report_id TEXT, blocked_reason TEXT, stalled_at TEXT,
      created_by TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(crew_id, number))""",
    "CREATE INDEX idx_tasks_status ON crew_tasks(crew_id, status)",
    "CREATE INDEX idx_tasks_owner ON crew_tasks(owner_session_id)",
    """CREATE TABLE crew_task_deps (crew_id TEXT NOT NULL, task_id TEXT, depends_on_id TEXT,
      PRIMARY KEY(crew_id, task_id, depends_on_id))""",
    """CREATE TABLE crew_checkpoints (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT,
      trigger TEXT NOT NULL, facts TEXT, facts_hash TEXT NOT NULL, headline TEXT, facts_source TEXT NOT NULL,
      compacted INTEGER NOT NULL DEFAULT 0, memory_id TEXT, seq INTEGER, created_at TEXT NOT NULL,
      run_id TEXT, state_before_ref TEXT, decision_ids TEXT, quality TEXT, confidence REAL, continuity_seq INTEGER,
      UNIQUE(session_id, facts_hash))""",
    "CREATE INDEX idx_ckp_session ON crew_checkpoints(crew_id, session_id, created_at)",
    "CREATE INDEX idx_ckp_task ON crew_checkpoints(task_id, created_at)",
    """CREATE TABLE crew_reports (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT,
      kind TEXT NOT NULL CHECK(kind IN ('completion','partial','stalled','waived')), verdict TEXT,
      criteria TEXT, commits TEXT, files TEXT, out_of_zone_files TEXT, tests TEXT, deploy TEXT,
      sections TEXT, summary TEXT, grounding TEXT, facts_source TEXT NOT NULL, facts_hash TEXT,
      review_state TEXT, reviewed_by TEXT, review_note TEXT, is_current INTEGER NOT NULL DEFAULT 1, superseded_reason TEXT,
      handoff_id TEXT, baton_ref TEXT, memory_id TEXT, created_at TEXT NOT NULL,
      UNIQUE(task_id, session_id, facts_hash))""",
    "CREATE UNIQUE INDEX uq_report_current ON crew_reports(task_id) WHERE is_current=1",
    """CREATE TABLE crew_batons (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, task_id TEXT, from_session TEXT,
      to_session TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('adopt','handover','same_checkout','human_assign','reserved_for','first_write')),
      offer_id TEXT, handoff_id TEXT, checkpoint_id TEXT, report_id TEXT, baton_ref TEXT, restored INTEGER,
      zone_ids TEXT, brief_text TEXT, seq INTEGER, created_at TEXT NOT NULL)""",
    "CREATE INDEX idx_batons_task ON crew_batons(task_id, created_at)",
    """CREATE TABLE crew_baton_offers (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, claim_id TEXT NOT NULL, task_id TEXT,
      to_session TEXT NOT NULL, via TEXT NOT NULL CHECK(via IN ('brief','human','reserved_for')), created_at TEXT NOT NULL,
      used_at TEXT, UNIQUE(claim_id, to_session))""",
]

_V1_CHANNEL_DECISIONS = [
    """CREATE TABLE crew_messages (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, seq INTEGER, thread_root_id TEXT,
      reply_to_id TEXT,
      kind TEXT NOT NULL, author_kind TEXT NOT NULL, author_user_id TEXT, author_session_id TEXT, author_agent_id TEXT,
      author_verified INTEGER NOT NULL DEFAULT 0,
      body TEXT NOT NULL, mentions TEXT, refs TEXT, client_msg_id TEXT, trust_score REAL, pinned INTEGER DEFAULT 0,
      edited_at TEXT, redacted INTEGER DEFAULT 0, redacted_body_hash TEXT, created_at TEXT NOT NULL,
      UNIQUE(crew_id, author_user_id, client_msg_id))""",
    "CREATE INDEX idx_msgs_seq ON crew_messages(crew_id, seq)",
    "CREATE INDEX idx_msgs_thread ON crew_messages(thread_root_id, seq)",
    """CREATE TABLE crew_message_edits (message_id TEXT NOT NULL, crew_id TEXT NOT NULL, prev_body TEXT NOT NULL,
      edited_at TEXT NOT NULL)""",
    """CREATE TABLE crew_proposals (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, message_id TEXT,
      title TEXT, body TEXT, options TEXT NOT NULL, quorum TEXT NOT NULL, eligible TEXT NOT NULL, deadline_at TEXT,
      state TEXT NOT NULL, outcome TEXT, decided_by TEXT, decision_id TEXT, created_at TEXT, closed_at TEXT,
      UNIQUE(crew_id, number))""",
    """CREATE TABLE crew_votes (crew_id TEXT NOT NULL, proposal_id TEXT, voter_kind TEXT, voter_id TEXT,
      voter_verified INTEGER NOT NULL DEFAULT 0,
      choice TEXT, blocking INTEGER DEFAULT 0, rationale TEXT, created_at TEXT,
      PRIMARY KEY(proposal_id, voter_kind, voter_id))""",
    """CREATE TABLE crew_decisions (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT,
      decision TEXT,
      rationale TEXT, alternatives TEXT, decided_by_kind TEXT, decided_by TEXT, participants TEXT,
      source TEXT CHECK(source IN ('direct','proposal','override')), task_id TEXT, zone_id TEXT, supersedes_id TEXT,
      state TEXT NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','in_force','rejected','superseded')),
      confirmed_by TEXT, confirmed_at TEXT, memory_id TEXT, created_at TEXT,
      evidence TEXT, proposed_by_verified INTEGER, decided_by_verified INTEGER, intent_version INTEGER,
      UNIQUE(crew_id, number))""",
]

_V1_INBOX_BYPASS = [
    """CREATE TABLE crew_inbox_items (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL,
      audience TEXT NOT NULL CHECK(audience IN ('project','crew','session')), recipient TEXT,
      kind TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'server' CHECK(origin IN ('server','human','agent')),
      ref_type TEXT, ref_id TEXT, priority INTEGER NOT NULL DEFAULT 2, title TEXT NOT NULL,
      primary_action TEXT, state TEXT NOT NULL CHECK(state IN ('open','seen','claimed','resolved','dismissed')),
      claimed_by TEXT, dedupe_key TEXT NOT NULL, coalesced_count INTEGER NOT NULL DEFAULT 1,
      created_seq INTEGER, resolved_seq INTEGER, resolved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX uq_inbox_open ON crew_inbox_items(crew_id, dedupe_key) WHERE state IN ('open','seen','claimed')",
    "CREATE INDEX idx_inbox_aud ON crew_inbox_items(crew_id, audience, recipient, state)",
    """CREATE TABLE crew_read_cursors (crew_id TEXT, principal TEXT, stream TEXT, last_seq INTEGER, updated_at TEXT,
      PRIMARY KEY(crew_id, principal, stream))""",
    """CREATE TABLE crew_bypass_codes (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, code_hash TEXT NOT NULL, session_id TEXT,
      scope TEXT NOT NULL, issued_by TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL)""",
]

_V1_EVENTS = [
    # actor / refs (JSON) are beyond the §3.2 DDL: the hash chain covers the full
    # envelope (callsign, agent_id, verified flag, every ref), which the scalar
    # columns cannot rebuild for replay or chain verification. remembra.crew.events
    # stores both and check_schema() refuses to start without them.
    """CREATE TABLE crew_events (crew_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
      project_id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, v INTEGER NOT NULL DEFAULT 1,
      actor_kind TEXT, actor_id TEXT, session_id TEXT, task_id TEXT, zone_id TEXT, ref_id TEXT,
      severity TEXT NOT NULL DEFAULT 'info', moment INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL,
      payload TEXT NOT NULL, idem_key TEXT, origin TEXT NOT NULL DEFAULT 'server', prev_hash TEXT, hash TEXT,
      actor TEXT NOT NULL DEFAULT '{}', refs TEXT NOT NULL DEFAULT '{}',
      UNIQUE(crew_id, seq))""",
    "CREATE UNIQUE INDEX uq_events_idem ON crew_events(crew_id, idem_key) WHERE idem_key IS NOT NULL",
    "CREATE INDEX idx_events_moment ON crew_events(crew_id, moment, seq)",
    "CREATE INDEX idx_events_task ON crew_events(task_id, seq)",
    "CREATE INDEX idx_events_sess ON crew_events(session_id, seq)",
    "CREATE TABLE crew_digests (crew_id TEXT, day TEXT, counts TEXT, moments TEXT, PRIMARY KEY(crew_id, day))",
    # Beyond the §3.2 DDL: every run of seqs retention deleted, with the hash-chain links on both
    # sides (prev_hash of the first pruned event, hash of the last). verify_crew_chain accepts a gap
    # only when it matches one of these ranges exactly, so a deleted event is detectable (§4.1).
    """CREATE TABLE crew_pruned_ranges (crew_id TEXT NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
      prev_hash TEXT NOT NULL, last_hash TEXT NOT NULL, pruned_at TEXT NOT NULL, PRIMARY KEY(crew_id, first_seq))""",
    # Account erasure (R-23): an event whose content was replaced by the tombstone, with the chain links
    # it had. verify_crew_chain accepts it only when the row still has those links and holds exactly the
    # tombstone (remembra.crew.events.tombstone_events). Added to v1 before crew.db first deployed.
    """CREATE TABLE crew_event_tombstones (crew_id TEXT NOT NULL, seq INTEGER NOT NULL, prev_hash TEXT NOT NULL,
      hash TEXT NOT NULL, reason TEXT NOT NULL, tombstoned_at TEXT NOT NULL, PRIMARY KEY(crew_id, seq))""",
]

_V1_OUTBOX_IDEMPOTENCY_NOTIFY = [
    # crew_outbox: spec columns plus next_attempt_at / last_error / updated_at,
    # which the worker needs for persistent retry backoff and a dead-letter reason.
    """CREATE TABLE crew_outbox (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','failed')),
      attempts INTEGER NOT NULL DEFAULT 0, result_id TEXT, created_at TEXT NOT NULL,
      next_attempt_at TEXT, last_error TEXT, updated_at TEXT)""",
    "CREATE INDEX idx_outbox_due ON crew_outbox(state, next_attempt_at)",
    """CREATE TABLE crew_idempotency (key TEXT PRIMARY KEY, principal TEXT NOT NULL, response_json TEXT NOT NULL,
      created_at TEXT NOT NULL)""",
    "CREATE INDEX idx_crew_idempotency_created ON crew_idempotency(created_at)",
    """CREATE TABLE crew_notification_rules (user_id TEXT, crew_id TEXT, kind TEXT, channel TEXT, quiet_hours TEXT,
      batch_window_s INTEGER DEFAULT 120, PRIMARY KEY(user_id, crew_id, kind))""",
    """CREATE TABLE crew_notify_targets (id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('email','webhook')),
      target TEXT NOT NULL, secret_hash TEXT, verified_at TEXT, created_at TEXT NOT NULL)""",
]

CREW_MIGRATIONS: list[Migration] = [
    (
        1,
        "crew_schema_v1",
        [
            *_V1_CREW_AND_MEMBERSHIP,
            *_V1_HOSTS_AND_SESSIONS,
            *_V1_ZONES,
            *_V1_CLAIMS_FOOTPRINTS_COLLISIONS,
            *_V1_TASKS_REPORTS_BATONS,
            *_V1_CHANNEL_DECISIONS,
            *_V1_INBOX_BYPASS,
            *_V1_EVENTS,
            *_V1_OUTBOX_IDEMPOTENCY_NOTIFY,
        ],
    ),
]

CREW_MIGRATION_RUNNER = MigrationRunner(CREW_MIGRATIONS, label="crew")

# Every table v1 creates, in creation order (tests and backup tooling use it).
CREW_TABLES: tuple[str, ...] = (
    "crews",
    "crew_members",
    "project_shares",
    "crew_hosts",
    "crew_sessions",
    "crew_zones",
    "crew_zone_overlaps",
    "crew_zone_files",
    "crew_zone_changes",
    "crew_repo_trees",
    "crew_claims",
    "crew_footprints",
    "crew_collisions",
    "crew_tasks",
    "crew_task_deps",
    "crew_checkpoints",
    "crew_reports",
    "crew_batons",
    "crew_baton_offers",
    "crew_messages",
    "crew_message_edits",
    "crew_proposals",
    "crew_votes",
    "crew_decisions",
    "crew_inbox_items",
    "crew_read_cursors",
    "crew_bypass_codes",
    "crew_events",
    "crew_digests",
    "crew_pruned_ranges",
    "crew_event_tombstones",
    "crew_outbox",
    "crew_idempotency",
    "crew_notification_rules",
    "crew_notify_targets",
)


def resolve_crew_db_path(main_db_path: str, override: str | None = None) -> str:
    """Where ``crew.db`` lives.

    ``override`` (or ``$REMEMBRA_CREW_DB_PATH``) wins. Otherwise it sits next to
    the main database file, so the same volume, backup and restore cover both.
    An in-memory main database gets an in-memory crew database.
    """
    chosen = override if override is not None else os.environ.get(CREW_DB_PATH_ENV)
    if chosen:
        return chosen
    path = main_db_path.split("///")[-1] if main_db_path.startswith("sqlite") else main_db_path
    if path in ("", ":memory:") or path.startswith("file::memory:"):
        return ":memory:"
    return str(Path(path).expanduser().parent / CREW_DB_FILENAME)


class CrewDatabase:
    """The ``crew.db`` connection: its own aiosqlite connection and its own lock."""

    def __init__(self, db_path: str) -> None:
        if db_path.startswith("sqlite"):
            db_path = db_path.split("///")[-1]
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None
        self._tx = TxCoordinator()
        # Distinct from the short SQLite transaction lock: handlers may await I/O.
        # File-backed databases also use an OS lock across connections/processes.
        self.outbox_run_lock = asyncio.Lock()
        self._guarded: GuardedConnection | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            return
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self.conn.execute(f"PRAGMA busy_timeout = {CREW_BUSY_TIMEOUT_MS}")
        await self._enable_wal()
        await self.conn.execute("PRAGMA synchronous = NORMAL")
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA temp_store = MEMORY")
        await self.conn.commit()
        log.info("crew_database_connected", path=self.db_path)

    async def _enable_wal(self) -> None:
        # Switching a fresh file to WAL needs an exclusive lock and SQLite does not
        # always run the busy handler for it, so two processes opening a new
        # crew.db at once can get "database is locked". Retry within the busy timeout.
        deadline = time.monotonic() + CREW_BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                await self.conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def init_schema(self) -> list[int]:
        """Connect if needed and apply :data:`CREW_MIGRATIONS`; returns versions applied now."""
        await self.connect()
        return await CREW_MIGRATION_RUNNER.apply(self.conn, self.transaction)

    async def get_schema_version(self) -> int:
        return await CREW_MIGRATION_RUNNER.current_version(self.conn)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._guarded = None
            log.info("crew_database_closed", path=self.db_path)

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    @property
    def conn(self) -> aiosqlite.Connection:
        """The crew connection, guarded so statements never interleave with an open transaction."""
        if self._connection is None:
            raise RuntimeError("crew database not connected; call connect() first")
        guarded = self._guarded
        if guarded is None or guarded.raw is not self._connection:
            guarded = GuardedConnection(self._connection, self._tx)
            self._guarded = guarded
        return guarded  # type: ignore[return-value]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` (``ROLLBACK`` on any exception) on the crew connection.

        Nested calls join the outer transaction. Only crew writes wait on this
        lock; memory writes on the main database never do.
        """
        if self._connection is None:
            raise RuntimeError("crew database not connected; call connect() first")
        async with self._tx.transaction(self._connection):
            yield

    @property
    def in_transaction(self) -> bool:
        return self._tx.in_transaction

    @asynccontextmanager
    async def read_snapshot(self) -> AsyncIterator[aiosqlite.Connection]:
        """Read committed WAL state without joining the writer queue.

        Timer scans use short read-only snapshots. Decisions to mutate must
        re-read their subjects inside the normal serialized write transaction.
        In-memory databases and callers already owning a transaction retain
        the existing connection so uncommitted state is never lost.
        """
        if not self.is_connected:
            raise RuntimeError("crew database not connected; call connect() first")
        if self.db_path == ":memory:" or self._tx.owns():
            async with self.transaction():
                yield self.conn
            return
        uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as reader:
            reader.row_factory = aiosqlite.Row
            await reader.execute(f"PRAGMA busy_timeout = {CREW_BUSY_TIMEOUT_MS}")
            await reader.execute("PRAGMA query_only = ON")
            await reader.execute("BEGIN")
            try:
                yield reader
            finally:
                await reader.rollback()

    def after_commit(self, callback: AfterCommit) -> bool:
        """Run ``callback`` after the crew transaction the caller owns commits (see ``TxCoordinator.after_commit``)."""
        return self._tx.after_commit(callback)

    async def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]


async def open_crew_db(main_db_path: str, override: str | None = None) -> CrewDatabase:
    """Open ``crew.db`` next to the main database (see :func:`resolve_crew_db_path`) and migrate it."""
    db = CrewDatabase(resolve_crew_db_path(main_db_path, override))
    try:
        await db.init_schema()
    except BaseException:
        await db.close()
        raise
    return db
