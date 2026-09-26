"""Row → view converters for ``crew.db`` rows (spec §4.1 entity views, ``schemas.VIEWS``).

Snapshots, event payloads and the per-agent page all carry the same entity views
(``SessionView``, ``ClaimView``, ``TaskView``, …). This module is the one place
that turns a ``crew.db`` row into its view, so the shapes cannot drift between
the snapshot (WP-8) and the events other services emit. Every function returns
exactly the keys of its ``schemas`` shape: never a token, token hash, raw
command, host path or any column the contract does not name.

The functions are pure (no I/O). ``now`` is only used for derived flags such as
``ClaimView.fenced``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas

# The holder stops own-claim writes this long before lease expiry (D31).
FENCE_MARGIN: Final = timedelta(seconds=60)

Row = Mapping[str, Any]


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, list | dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _str_list(value: Any) -> list[str]:
    return [v for v in _json(value, []) if isinstance(v, str)]


def _bool(value: Any) -> bool:
    return bool(value) and value not in ("0", "false")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def crew_mode(live_sessions: int) -> str:
    """``solo`` with ≤1 live session, ``multi`` with ≥2 (§2)."""
    return "multi" if live_sessions >= 2 else "solo"


def crew_view(row: Row, *, live_sessions: int, enforcement: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row.get("name"),
        "mode": crew_mode(live_sessions),
        "enforcement": enforcement,
        "settings_version": _int(row.get("settings_version"), 1),
        "last_seq": _int(row.get("last_seq")),
    }


def limit_view(row: Row) -> dict[str, Any] | None:
    level = row.get("limit_level")
    source = row.get("limit_source")
    if level not in schemas.LIMIT_LEVELS or source not in schemas.LIMIT_SOURCES:
        return None
    pct = row.get("limit_pct")
    return {"level": level, "pct": float(pct) if isinstance(pct, int | float) else None, "source": source}


def session_view(row: Row) -> dict[str, Any]:
    client_kind = row.get("client_kind")
    githook = row.get("githook_state")
    quiet = row.get("quiet_reason")
    return {
        "id": row["id"],
        "callsign": row["callsign"],
        "agent_id": row["agent_id"],
        "member_key": row["member_key"],
        "agent_verified": _bool(row.get("agent_verified")),
        "adapter": row.get("adapter"),
        "adapter_enforcement": row.get("adapter_enforcement") or "advisory",
        "client_kind": client_kind if client_kind in schemas.CLIENT_KINDS else None,
        "model": row.get("model"),
        "host_id": row.get("host_id"),
        "state": row["state"],
        "quiet_reason": quiet if quiet in schemas.QUIET_REASONS else None,
        "state_reason": row.get("state_reason"),
        "stuck": _bool(row.get("stuck")),
        "branch": row.get("branch"),
        "head_commit": row.get("head_commit"),
        "worktree_id": row.get("worktree_id"),
        "githook_state": githook if githook in schemas.GITHOOK_STATES else None,
        "current_task_id": row.get("current_task_id"),
        "limit": limit_view(row),
        "joined_at": row["joined_at"],
        "last_activity_at": row.get("last_activity_at"),
        "ended_at": row.get("ended_at"),
        "end_reason": row.get("end_reason"),
        "provider": row.get("provider"),
        "parent_session_id": row.get("parent_session_id") if schemas.is_id("session", row.get("parent_session_id")) else None,
        "sub_agent_id": row.get("sub_agent_id"),
    }


def claim_fenced(row: Row, now: datetime) -> bool:
    """True when the holder's own-write horizon (lease − 60 s) has passed for an active claim (D31)."""
    if row.get("state") != "active" or row.get("holder_kind") != "session":
        return False
    expires = _parse_ts(row.get("lease_expires_at"))
    return expires is not None and now >= expires - FENCE_MARGIN


def claim_view(row: Row, now: datetime) -> dict[str, Any]:
    reserve_reason = row.get("reserve_reason")
    return {
        "id": row["id"],
        "zone_id": row.get("zone_id"),
        "path_glob": row.get("path_glob"),
        "resource": row.get("resource"),
        "mode": row["mode"],
        "holder_kind": row["holder_kind"],
        "holder_session_id": row.get("holder_session_id"),
        "holder_agent_id": row.get("holder_agent_id"),
        "holder_user_id": row.get("holder_user_id"),
        "task_id": row.get("task_id"),
        "state": row["state"],
        "source": row["source"],
        "epoch": _int(row.get("epoch"), 1),
        "unconfirmed": _bool(row.get("unconfirmed")),
        "fenced": claim_fenced(row, now),
        "lease_expires_at": row.get("lease_expires_at"),
        "reserve_reason": reserve_reason if reserve_reason in schemas.RESERVE_REASONS else None,
        "reserved_for": row.get("reserved_for"),
        "offered_to": row.get("offered_to"),
        "queue_pos": row.get("queue_pos") if _int(row.get("queue_pos")) >= 1 else None,
        "baton_ref": row.get("baton_ref"),
        "granted_at": row.get("granted_at"),
        "version": _int(row.get("version"), 1),
    }


def _mcp_rules(value: Any) -> list[dict[str, Any]]:
    rules = []
    for item in _json(value, []):
        if isinstance(item, dict) and isinstance(item.get("tool"), str):
            service = item.get("service")
            rules.append({"tool": item["tool"], "service": service if isinstance(service, str) else None})
        elif isinstance(item, str):
            rules.append({"tool": item, "service": None})
    return rules


def zone_view(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row.get("title") or row["slug"],
        "parent_id": row.get("parent_id"),
        "is_leaf": _bool(row.get("is_leaf")),
        "builtin": _bool(row.get("builtin")),
        "include_globs": _str_list(row.get("include_globs")),
        "exclude_globs": _str_list(row.get("exclude_globs")),
        "services": _str_list(row.get("services")),
        "command_patterns": _str_list(row.get("command_patterns")),
        "mcp_tools": _mcp_rules(row.get("mcp_tools")),
        "mode": row.get("mode") or "exclusive",
        "auto_claim": _bool(row.get("auto_claim")),
        "protected": _bool(row.get("protected")),
        "reserve_for": row.get("reserve_for"),
        "fail_closed": _bool(row.get("fail_closed")),
        "frozen_by": row.get("frozen_by"),
        "frozen_note": row.get("frozen_note"),
        "frozen_until": row.get("frozen_until"),
        "source": row["source"],
        "version": _int(row.get("version"), 1),
    }


def commons_entries(value: Any) -> list[dict[str, Any]]:
    """``crew_zone_files.commons`` as ``CommonsEntry`` items (a bare glob string means ``plain``)."""
    out: list[dict[str, Any]] = []
    for item in _json(value, []):
        if isinstance(item, str):
            out.append({"glob": item, "kind": "plain"})
        elif isinstance(item, dict) and isinstance(item.get("glob"), str):
            kind = item.get("kind")
            out.append({"glob": item["glob"], "kind": kind if kind in schemas.COMMONS_KINDS else "plain"})
    return out


def _criteria(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _json(value, []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        out.append(
            {
                "id": item["id"],
                "text": str(item.get("text") or ""),
                "kind": item.get("kind") if item.get("kind") in schemas.CRITERION_KINDS else "manual",
                "match": item.get("match") if isinstance(item.get("match"), str) else None,
                "url": item.get("url") if isinstance(item.get("url"), str) else None,
                "required": bool(item.get("required", True)),
            }
        )
    return out


def task_view(row: Row, depends_on: Sequence[str] = ()) -> dict[str, Any]:
    before = row.get("status_before_stall")
    return {
        "id": row["id"],
        "number": _int(row.get("number"), 1),
        "title": row.get("title") or "",
        "status": row["status"],
        "status_before_stall": before if before in schemas.TASK_STATUSES else None,
        "phase": row.get("phase"),
        "priority": min(4, max(0, _int(row.get("priority"), 2))),
        "zone_ids": _str_list(row.get("zone_ids")),
        "owner_session_id": row.get("owner_session_id"),
        "owner_agent_id": row.get("owner_agent_id"),
        "reviewer": row.get("reviewer"),
        "depends_on": list(depends_on),
        "acceptance": _criteria(row.get("acceptance")),
        "acceptance_locked": _bool(row.get("acceptance_locked")),
        "started_head": row.get("started_head"),
        "current_report_id": row.get("current_report_id"),
        "blocked_reason": row.get("blocked_reason"),
        "version": _int(row.get("version"), 1),
    }


def task_ref(number: Any) -> str:
    """Display id of a task: ``T-14``."""
    return f"T-{_int(number, 0)}"


def collision_view(row: Row) -> dict[str, Any]:
    attribution = row.get("attribution")
    severity = row.get("severity") or schemas.COLLISION_SEVERITY.get(row["kind"], "notice")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "severity": severity,
        "subject": row["subject"],
        "zone_id": row.get("zone_id"),
        "session_a": row.get("session_a"),
        "session_b": row.get("session_b"),
        "claim_id": row.get("claim_id"),
        "attribution": attribution if attribution in schemas.ATTRIBUTIONS else None,
        "state": row["state"],
        # Critical and high kinds escalate to Needs-you (§5.3).
        "escalated": severity in ("high", "critical"),
        "resolution": row.get("resolution"),
    }


def decision_view(row: Row) -> dict[str, Any]:
    source = row.get("source")
    by_kind = row.get("decided_by_kind")
    return {
        "id": row["id"],
        "number": _int(row.get("number"), 1),
        "title": row.get("title") or "",
        "decision": row.get("decision") or "",
        "state": row["state"],
        "source": source if source in schemas.DECISION_SOURCES else "direct",
        "decided_by_kind": by_kind if by_kind in schemas.AUTHOR_KINDS else "agent",
        "decided_by": row.get("decided_by") or "",
        "confirmed_by": row.get("confirmed_by"),
        "task_id": row.get("task_id"),
        "zone_id": row.get("zone_id"),
        "supersedes_id": row.get("supersedes_id"),
    }


def offer_view(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "claim_id": row["claim_id"],
        "task_id": row.get("task_id"),
        "to_session": row["to_session"],
        "via": row["via"],
    }


def footprint_view(row: Row) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "worktree_id": row.get("worktree_id"),
        "path": row["path"],
        "state": row["state"],
        "attribution": row["attribution"],
    }


def _criterion_results(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _json(value, []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        status = item.get("status")
        source = item.get("source")
        out.append(
            {
                "id": item["id"],
                "status": status if status in schemas.CRITERION_STATUSES else "unknown",
                "source": source if source in schemas.FACTS_SOURCES else None,
            }
        )
    return out


def report_view(row: Row) -> dict[str, Any]:
    verdict = row.get("verdict")
    review = row.get("review_state")
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "session_id": row.get("session_id"),
        "kind": row["kind"],
        "verdict": verdict if verdict in schemas.REPORT_VERDICTS else None,
        "review_state": review if review in schemas.REVIEW_STATES else None,
        "is_current": _bool(row.get("is_current")),
        "superseded_reason": row.get("superseded_reason"),
        "facts_source": row["facts_source"],
        "criteria": _criterion_results(row.get("criteria")),
        "baton_ref": row.get("baton_ref"),
        "handoff_id": row.get("handoff_id"),
    }


def checkpoint_view(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "task_id": row.get("task_id"),
        "trigger": row["trigger"],
        "headline": (row.get("headline") or "")[:200],
        "facts_source": row["facts_source"],
    }


def baton_row_view(row: Row) -> dict[str, Any]:
    """A ``crew_batons`` row for the dashboard (not a contract view: it carries the brief text the adopter received)."""
    return {
        "id": row["id"],
        "task_id": row.get("task_id"),
        "from_session": row.get("from_session"),
        "to_session": row["to_session"],
        "kind": row["kind"],
        "offer_id": row.get("offer_id"),
        "handoff_id": row.get("handoff_id"),
        "checkpoint_id": row.get("checkpoint_id"),
        "report_id": row.get("report_id"),
        "baton_ref": row.get("baton_ref"),
        "restored": None if row.get("restored") is None else _bool(row.get("restored")),
        "zone_ids": _str_list(row.get("zone_ids")),
        "brief_text": row.get("brief_text"),
        "seq": row.get("seq"),
        "created_at": row["created_at"],
    }
