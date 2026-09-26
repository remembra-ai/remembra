"""Account erasure for ``crew.db`` (R-23 with Crew mode on).

The main database's :class:`remembra.account.erasure.AccountEraser` erases
another SQLite file only through an :class:`~remembra.account.erasure.ExtraDatabase`
carrying explicit rules: the generic scan cannot tell a row an account owns
from a row it only acted on in someone else's crew, nor find content keyed by
a crew session or message id. These are those rules.

For the erased account:

* every crew it owns goes, with every row of those crews (events, sessions,
  claims, tasks, messages, reports, zones, outbox, digests, …);
* in crews owned by someone else (a team crew it was a member of), its own
  rows go: its crew sessions and everything keyed by them (checkpoints,
  footprints, reports, claims, batons and offers, votes, bypass codes, read
  cursors), the messages it wrote (with their edit history, and the proposals
  made in them with their votes; others' replies lose the link), its membership,
  its inbox items and idempotency records. Rows of the owner's record it only
  acted on stay, with the actor column cleared (the task it created or owned,
  the decision it confirmed, the zone it froze, the review it made, the
  collision its session was part of). The owner's hash-chained event log is
  not rewritten: it is the owner's record of their crew;
* its hosts, notification targets and rules, and the project shares it made.

``crew.db`` is wired into the app's eraser by the ``crew.db`` startup hook
(:mod:`remembra.crew.db_hook`); ``tests/crew/test_crew_erasure.py`` runs
:func:`~remembra.account.erasure.registry_problems` over the real migrated
schema, so a crew table added later fails that test until it has a rule.
"""

from __future__ import annotations

from typing import Any

from remembra.account.erasure import ExtraDatabase, TableRule

# Subqueries: crews the account owns, its crew sessions (in any crew), messages it wrote.
_CREWS = "SELECT id FROM crews WHERE owner_user_id = :uid"
_SESSIONS = "SELECT id FROM crew_sessions WHERE user_id = :uid"
_MESSAGES = "SELECT id FROM crew_messages WHERE author_user_id = :uid"
_PROPOSALS = f"SELECT id FROM crew_proposals WHERE message_id IN ({_MESSAGES})"

_IN_CREWS = f"crew_id IN ({_CREWS})"


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
        deletes=(_IN_CREWS, "recipient = :uid", f"recipient IN ({_SESSIONS})"),
        nulls=(
            ("claimed_by", "claimed_by = :uid"),
            ("claimed_by", f"claimed_by IN ({_SESSIONS})"),
            ("resolved_by", "resolved_by = :uid"),
            ("resolved_by", f"resolved_by IN ({_SESSIONS})"),
        ),
    ),
    TableRule("crew_read_cursors", deletes=(_IN_CREWS, "principal = :uid", f"principal IN ({_SESSIONS})")),
    TableRule("crew_idempotency", deletes=("principal = :uid", f"principal IN ({_SESSIONS})")),
    TableRule("crew_baton_offers", deletes=(_IN_CREWS, f"to_session IN ({_SESSIONS})")),
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
    TableRule(
        "crew_decisions",
        deletes=(_IN_CREWS,),
        nulls=(
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
    _crew_only("crew_outbox"),
    _crew_only("crew_pruned_ranges"),
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

CREW_EXEMPT_TABLES: dict[str, str] = {
    "schema_version": "applied crew.db migrations",
    "sqlite_sequence": "SQLite internal",
    "sqlite_stat1": "SQLite internal",
}


def crew_extra_database(db: Any) -> ExtraDatabase:
    """``crew.db`` as the account eraser covers it (its rules and exemptions)."""
    return ExtraDatabase(name="crew", db=db, rules=CREW_ERASURE_RULES, exempt=CREW_EXEMPT_TABLES)
