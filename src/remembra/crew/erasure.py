"""Account erasure for ``crew.db`` (R-23 with Crew mode on).

The main database's :class:`remembra.account.erasure.AccountEraser` erases
another SQLite file only through an :class:`~remembra.account.erasure.ExtraDatabase`
carrying explicit rules: the generic scan cannot tell a row an account owns
from a row it only acted on in someone else's crew, nor find content keyed by
a crew session or message id. These are those rules.

For the erased account (:data:`CREW_TABLE_HOLDINGS` says, per table, what it
holds for a user and which of those rows are the account's own and which are
shared crew history):

* every crew it owns goes, with every row of those crews (events, sessions,
  claims, tasks, messages, reports, zones, outbox, digests, tombstones, …);
* in crews owned by someone else (a team crew it was a member of), its own
  rows go: its crew sessions (sub-agent sessions included: they are its own)
  and everything keyed by them (checkpoints, footprints, reports, claims,
  batons and offers, votes, bypass codes, read cursors, inbox items addressed
  to it or about it), the messages it wrote (with their edit history, and the
  proposals made in them with their votes; others' replies lose the link), the
  decisions it proposed that no human adopted, its membership, its idempotency
  records and outbox items carrying its work. Rows of the owner's record it only
  acted on are shared history: they stay, with the actor columns cleared (the
  task it created or owned, the decision in force it proposed or confirmed, the
  zone it froze, the review it made, the collision its session was part of);
* the owner's hash-chained event log keeps its chain: every event carrying the
  account's identity (user id, session, message or host id, email) is
  tombstoned by :func:`tombstone_account_events`. It keeps its seq, type, time
  and links and loses its actor, refs, summary and payload, and
  ``verify_crew_chain`` still passes;
* its hosts, notification targets and rules, and the project shares it made.

``crew.db`` is wired into the app's eraser by the ``crew.db`` startup hook
(:mod:`remembra.crew.db_hook`); ``tests/crew/test_crew_erasure.py`` runs
:func:`~remembra.account.erasure.registry_problems` over the real migrated
schema, so a crew table added later fails that test until it has a rule.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Final

from remembra.account.erasure import ExtraDatabase, TableRule
from remembra.crew.events import tombstone_events

# Subqueries: crews the account owns, its crew sessions (in any crew), messages it wrote.
_CREWS = "SELECT id FROM crews WHERE owner_user_id = :uid"
_SESSIONS = "SELECT id FROM crew_sessions WHERE user_id = :uid"
_MESSAGES = "SELECT id FROM crew_messages WHERE author_user_id = :uid"
_PROPOSALS = f"SELECT id FROM crew_proposals WHERE message_id IN ({_MESSAGES})"

_IN_CREWS = f"crew_id IN ({_CREWS})"
# Decisions the account proposed that no human adopted: its own text, erased with it.
_OWN_DECISIONS = (
    "SELECT id FROM crew_decisions WHERE state IN ('proposed', 'rejected')"
    f" AND (decided_by = :uid OR decided_by IN ({_SESSIONS}))"
)


def _crew_only(table: str) -> TableRule:
    return TableRule(table, deletes=(_IN_CREWS,))


# Children before parents: a clause selecting parent ids (crews, sessions, messages)
# must run while those rows still exist, so crew_messages, crew_sessions and crews come last.
CREW_ERASURE_RULES: tuple[TableRule, ...] = (
    TableRule("crew_message_edits", deletes=(_IN_CREWS, f"message_id IN ({_MESSAGES})")),
    TableRule(
        "crew_votes",
        deletes=(_IN_CREWS, "voter_id = :uid", f"voter_id IN ({_SESSIONS})", f"proposal_id IN ({_PROPOSALS})"),
    ),
    TableRule(
        "crew_inbox_items",
        deletes=(
            _IN_CREWS,
            "recipient = :uid",
            f"recipient IN ({_SESSIONS})",
            f"ref_id IN ({_SESSIONS})",
            f"ref_id IN ({_MESSAGES})",
            f"ref_id IN ({_OWN_DECISIONS})",
            # items raised by the account's own actions carry its id or session in their dedupe key
            "instr(dedupe_key, :uid) > 0",
            "EXISTS (SELECT 1 FROM crew_sessions s WHERE s.user_id = :uid AND instr(crew_inbox_items.dedupe_key, s.id) > 0)",
        ),
        nulls=(
            ("claimed_by", "claimed_by = :uid"),
            ("claimed_by", f"claimed_by IN ({_SESSIONS})"),
            ("resolved_by", "resolved_by = :uid"),
            ("resolved_by", f"resolved_by IN ({_SESSIONS})"),
        ),
    ),
    TableRule("crew_read_cursors", deletes=(_IN_CREWS, "principal = :uid", f"principal IN ({_SESSIONS})")),
    TableRule("crew_idempotency", deletes=("principal = :uid", f"principal IN ({_SESSIONS})")),
    TableRule(
        "crew_baton_offers",
        deletes=(
            _IN_CREWS,
            f"to_session IN ({_SESSIONS})",
            f"claim_id IN (SELECT id FROM crew_claims WHERE holder_user_id = :uid OR holder_session_id IN ({_SESSIONS}))",
        ),
    ),
    TableRule(
        "crew_batons",
        deletes=(_IN_CREWS, f"from_session IN ({_SESSIONS})", f"to_session IN ({_SESSIONS})"),
    ),
    TableRule("crew_bypass_codes", deletes=(_IN_CREWS, "issued_by = :uid", f"session_id IN ({_SESSIONS})")),
    TableRule("crew_checkpoints", deletes=(_IN_CREWS, f"session_id IN ({_SESSIONS})")),
    TableRule("crew_footprints", deletes=(_IN_CREWS, f"session_id IN ({_SESSIONS})")),
    TableRule(
        "crew_reports",
        deletes=(_IN_CREWS, f"session_id IN ({_SESSIONS})"),
        nulls=(("reviewed_by", "reviewed_by = :uid"),),
    ),
    TableRule(
        "crew_collisions",
        deletes=(_IN_CREWS,),
        nulls=(
            ("evidence", f"session_a IN ({_SESSIONS})"),
            ("evidence", f"session_b IN ({_SESSIONS})"),
            ("session_a", f"session_a IN ({_SESSIONS})"),
            ("session_b", f"session_b IN ({_SESSIONS})"),
            ("resolved_by", "resolved_by = :uid"),
        ),
    ),
    TableRule(
        "crew_claims",
        deletes=(_IN_CREWS, "holder_user_id = :uid", f"holder_session_id IN ({_SESSIONS})"),
    ),
    _crew_only("crew_task_deps"),
    TableRule(
        "crew_tasks",
        deletes=(_IN_CREWS,),
        nulls=(
            ("owner_user_id", "owner_user_id = :uid"),
            ("owner_agent_id", f"owner_session_id IN ({_SESSIONS})"),  # before owner_session_id is cleared
            ("owner_session_id", f"owner_session_id IN ({_SESSIONS})"),
            ("created_by", "created_by = :uid"),
            ("created_by", f"created_by IN ({_SESSIONS})"),
        ),
    ),
    # A decision the account proposed that no human adopted (proposed or rejected) is its own text and
    # goes; one in force or superseded is the crew owner's record and stays, with its proposer cleared.
    TableRule(
        "crew_decisions",
        deletes=(f"id IN ({_OWN_DECISIONS})",),
    ),
    TableRule(
        "crew_decisions",
        deletes=(_IN_CREWS,),
        nulls=(
            ("participants", "decided_by = :uid"),
            ("participants", f"decided_by IN ({_SESSIONS})"),
            ("decided_by", "decided_by = :uid"),
            ("decided_by", f"decided_by IN ({_SESSIONS})"),
            ("confirmed_by", "confirmed_by = :uid"),
        ),
    ),
    # A proposal is made in a message: the victim's proposal text goes with the message (and its votes above).
    TableRule(
        "crew_proposals",
        deletes=(_IN_CREWS, f"message_id IN ({_MESSAGES})"),
        nulls=(("decided_by", "decided_by = :uid"),),
    ),
    TableRule(
        "crew_zone_changes",
        deletes=(_IN_CREWS,),
        nulls=(
            ("uploaded_by_user", "uploaded_by_user = :uid"),
            ("uploaded_by_session", f"uploaded_by_session IN ({_SESSIONS})"),
            ("decided_by", "decided_by = :uid"),
        ),
    ),
    TableRule(
        "crew_zone_files",
        deletes=(_IN_CREWS,),
        nulls=(("uploaded_by", "uploaded_by = :uid"), ("uploaded_by", f"uploaded_by IN ({_SESSIONS})")),
    ),
    _crew_only("crew_zone_overlaps"),
    TableRule(
        "crew_zones",
        deletes=(_IN_CREWS,),
        nulls=(("created_by", "created_by = :uid"), ("frozen_by", "frozen_by = :uid")),
    ),
    _crew_only("crew_repo_trees"),
    _crew_only("crew_digests"),
    TableRule(
        "crew_outbox",
        deletes=(
            _IN_CREWS,
            "instr(payload, :uid) > 0",
            "EXISTS (SELECT 1 FROM crew_sessions s WHERE s.user_id = :uid AND instr(crew_outbox.payload, s.id) > 0)",
        ),
    ),
    _crew_only("crew_pruned_ranges"),
    _crew_only("crew_event_tombstones"),
    TableRule("crew_events", deletes=(_IN_CREWS, "owner_user_id = :uid")),
    TableRule("crew_notification_rules", deletes=(_IN_CREWS, "user_id = :uid")),
    TableRule("crew_notify_targets", deletes=("user_id = :uid",)),
    TableRule("crew_hosts", deletes=("user_id = :uid",)),
    TableRule(
        "crew_members",
        deletes=(_IN_CREWS, "user_id = :uid"),
        nulls=(("added_by", "added_by = :uid"),),
    ),
    TableRule("project_shares", deletes=("owner_user_id = :uid",), nulls=(("shared_by", "shared_by = :uid"),)),
    TableRule(
        "crew_messages",
        deletes=(_IN_CREWS, "author_user_id = :uid", f"author_session_id IN ({_SESSIONS})"),
        # Others' replies stay; the link to the erased message does not.
        nulls=(("reply_to_id", f"reply_to_id IN ({_MESSAGES})"), ("thread_root_id", f"thread_root_id IN ({_MESSAGES})")),
    ),
    TableRule("crew_sessions", deletes=(_IN_CREWS, "user_id = :uid")),
    TableRule("crews", deletes=("owner_user_id = :uid",)),
)

# What each crew table holds for a user, and which of those rows are the user's own (erased) and
# which are shared crew history (kept, with the user's actor columns cleared). The coverage test
# requires an entry for every table of the migrated schema.
CREW_TABLE_HOLDINGS: Final[Mapping[str, str]] = {
    "crews": "own: the crews the user owns (project crews) and everything in them",
    "crew_members": "own: the user's memberships; shared: members the user added (added_by cleared)",
    "project_shares": "own: shares of the user's projects; shared: shares the user made of others' (shared_by cleared)",
    "crew_hosts": "own: the user's machines (crewd hosts)",
    "crew_sessions": "own: the user's agent sessions, sub-agent sessions included",
    "crew_zones": "shared: zones of another owner's crew the user created or froze (created_by/frozen_by cleared)",
    "crew_zone_overlaps": "crew-only: derived from zones; no user data",
    "crew_zone_files": "shared: another owner's zones.yml the user uploaded (uploaded_by cleared)",
    "crew_zone_changes": "shared: zone changes the user uploaded or decided in another owner's crew (actors cleared)",
    "crew_repo_trees": "crew-only: a crew's repository tree; no user data",
    "crew_claims": "own: claims the user (or its sessions) held",
    "crew_footprints": "own: files the user's sessions touched",
    "crew_collisions": "shared: collisions the user's session was part of (session and evidence cleared)",
    "crew_tasks": "shared: tasks of another owner's crew the user created or owned (owner and creator cleared)",
    "crew_task_deps": "crew-only: task dependencies; no user data",
    "crew_checkpoints": "own: the user's sessions' checkpoints (their facts)",
    "crew_reports": "own: the user's sessions' reports; shared: reports it reviewed (reviewed_by cleared)",
    "crew_batons": "own: batons from or to the user's sessions",
    "crew_baton_offers": "own: offers to the user's sessions, and offers of claims it held",
    "crew_messages": "own: messages the user wrote (replies of others lose the link)",
    "crew_message_edits": "own: the edit history of the user's messages",
    "crew_proposals": "own: proposals made in the user's messages; shared: ones it decided (decided_by cleared)",
    "crew_votes": "own: the user's votes, and votes on its proposals",
    "crew_decisions": "own: decisions the user proposed that no human adopted; shared: decisions in force"
    " it proposed or confirmed (proposer, participants and confirmer cleared)",
    "crew_inbox_items": "own: items addressed to the user or its sessions, or about them; shared: items it claimed"
    " or resolved (claimed_by/resolved_by cleared)",
    "crew_read_cursors": "own: the user's read positions",
    "crew_bypass_codes": "own: codes the user issued or that were issued for its sessions",
    "crew_events": "own: the event log of the user's crews; shared: another owner's log keeps every event,"
    " and those carrying the user's identity are tombstoned (content removed, chain intact)",
    "crew_digests": "crew-only: per-day counts and moment seqs; no user data",
    "crew_pruned_ranges": "crew-only: chain links of pruned events; no user data",
    "crew_event_tombstones": "crew-only: chain links of tombstoned events; no user data",
    "crew_outbox": "own: pending and done work carrying the user's id or sessions (memory promotions)",
    "crew_idempotency": "own: the user's and its sessions' replay records",
    "crew_notification_rules": "own: the user's notification rules",
    "crew_notify_targets": "own: the user's email and webhook targets",
}

CREW_EXEMPT_TABLES: dict[str, str] = {
    "schema_version": "applied crew.db migrations",
    "sqlite_sequence": "SQLite internal",
    "sqlite_stat1": "SQLite internal",
}


# Ids that identify the account inside crews it does not own: its user id, its crew sessions,
# the messages it wrote and its hosts. An event of another owner's crew whose actor, refs,
# summary, payload or idempotency key holds any of them (or the account's email) is tombstoned.
_IDENTITY_QUERIES: Final = (
    "SELECT id FROM crew_sessions WHERE user_id = ?",
    "SELECT id FROM crew_messages WHERE author_user_id = ?"
    " OR author_session_id IN (SELECT id FROM crew_sessions WHERE user_id = ?)",
    "SELECT id FROM crew_hosts WHERE user_id = ?",
)
_EVENT_TEXT_COLUMNS: Final = ("actor", "refs", "payload", "summary", "COALESCE(idem_key, '')")
_TOKEN_CHUNK: Final = 40


async def _identity_tokens(conn: Any, user_id: str) -> list[str]:
    tokens: list[str] = [user_id]
    for query in _IDENTITY_QUERIES:
        cursor = await conn.execute(query, (user_id,) * query.count("?"))
        tokens.extend(str(r[0]) for r in await cursor.fetchall() if r[0])
    return list(dict.fromkeys(t for t in tokens if t))


async def tombstone_account_events(conn: Any, user_id: str, email: str | None) -> Mapping[str, int]:
    """Tombstone every event of a crew the account does not own that carries its identity (R-23).

    The account's own crews are deleted whole by :data:`CREW_ERASURE_RULES`. In another owner's
    crew its work stays in the hash-chained log as a tombstone: the event keeps its seq, type,
    time and chain links, and loses its actor, refs, summary and payload
    (:func:`remembra.crew.events.tombstone_events`), so ``verify_crew_chain`` still passes. Runs
    before the rules (they delete the rows the identity lookup reads), in the same transaction.
    """
    tokens = await _identity_tokens(conn, user_id)
    mail = email.strip().lower() if email else None
    found: dict[str, set[int]] = defaultdict(set)
    chunks = [tokens[i : i + _TOKEN_CHUNK] for i in range(0, len(tokens), _TOKEN_CHUNK)]
    for chunk in chunks + ([[]] if mail else []):
        clauses: list[str] = []
        params: list[Any] = [user_id]
        for token in chunk:
            for column in _EVENT_TEXT_COLUMNS:
                clauses.append(f"instr({column}, ?) > 0")
                params.append(token)
        if mail and not chunk:
            for column in _EVENT_TEXT_COLUMNS:
                clauses.append(f"instr(LOWER({column}), ?) > 0")
                params.append(mail)
        if not clauses:
            continue
        cursor = await conn.execute(
            "SELECT crew_id, seq FROM crew_events"
            " WHERE crew_id NOT IN (SELECT id FROM crews WHERE owner_user_id = ?) AND actor_id IS NOT 'erased'"
            f" AND ({' OR '.join(clauses)})",
            params,
        )
        for crew_id, seq in await cursor.fetchall():
            found[str(crew_id)].add(int(seq))
    total = 0
    for crew_id, seqs in sorted(found.items()):
        total += await tombstone_events(conn, crew_id, sorted(seqs), reason="account_erased")
    return {"crew_events_tombstoned": total}


def crew_extra_database(db: Any) -> ExtraDatabase:
    """``crew.db`` as the account eraser covers it (its rules, exemptions and event tombstones)."""
    return ExtraDatabase(
        name="crew", db=db, rules=CREW_ERASURE_RULES, exempt=CREW_EXEMPT_TABLES, prepare=tombstone_account_events
    )
