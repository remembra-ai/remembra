"""Server-side input for Marshal's "why?" verdict (:mod:`remembra.marshal.diagnosis`).

:func:`gather_diagnosis_input` reads what the dashboard's slip reads, with the
caller's own access: the newest 100 trail entries (with their pickups), the
agent's own newest 5, the agent's row of the 7-day activity summary and the
account's active keys. A project-restricted caller reads only its projects'
entries and sees key counts and times, never key names. Reads only: nothing
is recorded (no pickup, no binding, no key use).

:func:`evidence` is the numeric evidence block ``GET /trail/diagnosis`` returns
beside the verdict, from the same facts the verdict table decided on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from remembra.auth.middleware import AuthenticatedUser
from remembra.config import get_settings
from remembra.marshal import commands
from remembra.marshal.diagnosis import (
    SLIP_AGENT_LIMIT,
    SLIP_TRAIL_LIMIT,
    DiagnosisInput,
    KeyEvidence,
    adapter_id,
    canonical_agent_id,
    facts_of,
    parse_server_time,
)
from remembra.services.relay import RelayService

SUMMARY_DAYS = 7


def _service(state: Any) -> RelayService:
    return RelayService(
        db=state.db,
        memory_service=getattr(state, "memory_service", None),
        crew_db=getattr(state, "crew_db", None),
        crew_events=getattr(state, "crew_events", None),
    )


def utc_z(value: str | datetime | None) -> str | None:
    """A timestamp as ISO 8601 UTC with ``Z`` (None when it can't be read)."""
    parsed = parse_server_time(value)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


async def _trail_items(
    service: RelayService, user: AuthenticatedUser, limit: int, agent_id: str | None = None
) -> list[dict[str, Any]]:
    """The newest ``limit`` entries the caller may read (all projects, or each of its projects merged)."""
    if not user.project_ids:
        result = await service.trail(user.user_id, None, limit=limit, agent_id=agent_id)
        return list(result["items"])
    merged: dict[str, dict[str, Any]] = {}
    for project in user.project_ids:
        result = await service.trail(user.user_id, project, limit=limit, agent_id=agent_id, allowed=list(user.project_ids))
        for item in result["items"]:
            merged.setdefault(str(item.get("id")), item)

    def newest(item: dict[str, Any]) -> tuple[float, str]:
        at = parse_server_time(item.get("created_at"))
        return (at.timestamp() if at is not None else 0.0, str(item.get("id") or ""))

    return sorted(merged.values(), key=newest, reverse=True)[:limit]


@dataclass(frozen=True)
class AccountReads:
    """The reads every agent's verdict shares: the trail, the activity summary and the keys."""

    trail: list[dict[str, Any]]
    summary: dict[str, Any]
    keys: list[KeyEvidence]
    server_url: str

    def summary_agent(self, agent: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for row in self.summary.get("agents") or []:
            if canonical_agent_id(row.get("agent_id")) == agent:
                found = row  # the last match wins, as the dashboard's Map of rows does
        return found


async def read_account(state: Any, user: AuthenticatedUser, now: datetime) -> AccountReads:
    service = _service(state)
    trail = await _trail_items(service, user, SLIP_TRAIL_LIMIT)
    summary = await service.activity_summary(user.user_id, days=SUMMARY_DAYS, allowed=user.project_ids or None, now=now)
    keys: list[KeyEvidence] = []
    manager = getattr(state, "api_key_manager", None)
    if manager is not None:
        for key in await manager.list_keys(user.user_id):
            if not key.active:
                continue
            keys.append(
                KeyEvidence(
                    # Key names are the account's labels: a key restricted to projects sees counts and times only.
                    name=key.name if not user.project_ids else None,
                    created_at=utc_z(key.created_at),
                    last_used_at=utc_z(key.last_used_at),
                    active=True,
                )
            )
    return AccountReads(trail=trail, summary=summary, keys=keys, server_url=get_settings().public_url or commands.CLOUD_URL)


async def diagnosis_input_for(
    state: Any, user: AuthenticatedUser, agent_id: str, now: datetime, account: AccountReads
) -> DiagnosisInput:
    """One agent's input from the shared reads plus its own newest entries."""
    agent = canonical_agent_id(agent_id)
    agent_trail = await _trail_items(_service(state), user, SLIP_AGENT_LIMIT, adapter_id(agent))
    return DiagnosisInput(
        agent_id=agent,
        keys=account.keys,
        trail=account.trail,
        agent_trail=agent_trail,
        summary_agent=account.summary_agent(agent),
        now=now,
        server_url=account.server_url,
    )


async def gather_diagnosis_input(state: Any, user: AuthenticatedUser, agent_id: str, now: datetime) -> DiagnosisInput:
    """The slip's three reads plus the agent's summary row, as the caller may see them."""
    return await diagnosis_input_for(state, user, agent_id, now, await read_account(state, user, now))


def _epoch_seconds(value: str | None) -> float:
    parsed = parse_server_time(value)
    return parsed.timestamp() if parsed is not None else 0.0


def evidence(inp: DiagnosisInput) -> dict[str, Any]:
    """The counts and times the verdict rests on (``evidence`` of ``GET /trail/diagnosis``)."""
    facts = facts_of(inp)
    used = [k for k in inp.keys if k.active is not False and k.last_used_at]
    # The first newest, as the slip's keys line picks it (a stable sort, newest first).
    newest_key = max(used, key=lambda k: _epoch_seconds(k.last_used_at), default=None)
    newest_entry = facts.own[0].get("created_at") if facts.own else None
    if newest_entry is None and inp.summary_agent:
        newest_entry = inp.summary_agent.get("last_active")
    return {
        "keys": {
            "active": facts.active_keys,
            "used": facts.used_keys,
            "newest_used_at": utc_z(newest_key.last_used_at) if newest_key else None,
            "newest_name": newest_key.name if newest_key else None,
        },
        "entries": {"count": facts.entry_count, "handoffs": facts.own_handoffs, "newest_at": utc_z(newest_entry)},
        "pickups": {"briefs": facts.briefs, "others_handoffs": facts.others_handoffs, "trail_entries": facts.trail_entries},
    }
