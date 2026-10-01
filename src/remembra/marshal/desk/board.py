"""The desk's board: what needs a look, written by rules only (no model, no budget, nothing written).

For each relay agent the account has seen (on the 7-day activity summary, or
as a reader of a handoff among the newest 100 entries; at most six), the "why?"
verdict (:func:`remembra.marshal.diagnosis.diagnose_agent`) is worked out from
the same reads the slip makes. Agents that handed off are left out; a key
problem (no active key, keys never used) is one account-level call. At most
three calls are shown, proven ones first. The status line, the suggested
questions and the model's state (ready, limited or offline, and why) come
with them.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from remembra.auth.middleware import AuthenticatedUser
from remembra.cloud.metering import CreditPeriod
from remembra.marshal.desk import budget
from remembra.marshal.desk.constants import ASK_RESERVE_MICRO
from remembra.marshal.desk.llm import desk_breaker, offline_reason
from remembra.marshal.desk.logs import log
from remembra.marshal.diagnosis import Verdict, adapter_id, canonical_agent_id, diagnose_agent
from remembra.relay.adapters import REGISTRY
from remembra.services.marshal_diagnosis import diagnosis_input_for, read_account

FOOTER = "Built by rules from your keys, trail and inbox. No model wrote this."
MAX_AGENTS = 6
MAX_CALLS = 3
MAX_SUGGESTIONS = 3
MAX_FRAGMENTS = 3
FAIR_USE_SHOWN_AT = 50  # percent
ACCOUNT_CODES = ("KEY_MISSING", "KEY_NEVER_USED")
CODE_ORDER = (
    "KEY_MISSING",
    "KEY_NEVER_USED",
    "PICKS_UP_NEVER_CLOSES",
    "CODEX_TRUST_MISSING",
    "HOOKS_NOT_FIRING",
    "STALE_CHECKPOINT",
    "NOTHING_WAITING",
)


def ask_for(code: str, agent: str | None) -> str:
    """The question a call suggests (and asks, from the board's "see why")."""
    adapter = adapter_id(agent) if agent else ""
    if code in ACCOUNT_CODES:
        return "why can't my agents reach Remembra"
    if code == "PICKS_UP_NEVER_CLOSES":
        return f"why does {adapter} never close"
    if code == "STALE_CHECKPOINT":
        return f"why did {adapter} stop without a handoff"
    return f"why is {adapter} waiting"


def short_form(code: str, agent: str | None) -> str:
    adapter = adapter_id(agent) if agent else ""
    return {
        "KEY_MISSING": "no active key",
        "KEY_NEVER_USED": "keys never used",
        "PICKS_UP_NEVER_CLOSES": f"{adapter} never closes",
        "CODEX_TRUST_MISSING": "codex trust likely missing",
        "HOOKS_NOT_FIRING": f"{adapter} hooks likely not firing",
        "STALE_CHECKPOINT": f"{adapter} stopped without a handoff",
        "NOTHING_WAITING": f"{adapter} waiting",
    }[code]


def agents_seen(summary: dict[str, Any], trail: list[dict[str, Any]]) -> list[str]:
    """Relay agents on the activity summary, then readers of handoffs on the trail; at most six."""
    seen: list[str] = []
    candidates = [row.get("agent_id") for row in summary.get("agents") or []]
    candidates += [p.get("agent_id") for item in trail for p in item.get("picked_up_by") or []]
    for raw in candidates:
        agent = canonical_agent_id(raw)
        if agent in REGISTRY and agent not in seen:
            seen.append(agent)
    return seen[:MAX_AGENTS]


def _call(verdict: Verdict, agent: str | None) -> dict[str, Any]:
    return {
        "agent_id": agent,
        "code": verdict.code,
        "proven": verdict.proven,
        "text": verdict.verdict,
        "ask": ask_for(verdict.code, agent),
    }


def rank(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(calls, key=lambda c: (not c["proven"], CODE_ORDER.index(c["code"]), c["agent_id"] or ""))


def status_line(calls: list[dict[str, Any]], unread: int, fair_use: int | None, seen: bool) -> str:
    fragments: list[str] = []
    if calls:
        fragments.append(short_form(calls[0]["code"], calls[0]["agent_id"]))
        if len(calls) > 1:
            fragments.append(f"{len(calls) - 1} more to check")
    if unread > 0:
        fragments.append(f"{unread} unread in inbox")
    if fair_use is not None and fair_use >= FAIR_USE_SHOWN_AT:
        fragments.append(f"relay {fair_use}% of fair-use")
    if not fragments:
        return "every agent handing off" if seen else "no handoffs yet"
    return " · ".join(fragments[:MAX_FRAGMENTS])


def suggestions(
    _calls: list[dict[str, Any]], trail: list[dict[str, Any]], *, unread: int = 0, fair_use: int | None = None
) -> list[str]:
    out: list[str] = []
    if trail and trail[0].get("memory_type") == "handoff" and trail[0].get("agent_id"):
        out.append(f"what did {adapter_id(trail[0]['agent_id'])} hand off last")
    if unread > 0:
        out.append("how many unread inbox notes are waiting")
    if fair_use is not None and fair_use >= FAIR_USE_SHOWN_AT:
        out.append("how close is relay activity to fair-use")
    return list(dict.fromkeys(out))[:MAX_SUGGESTIONS]


async def _fair_use(state: Any, user_id: str, now: datetime) -> int | None:
    """Relay events this month as a whole percentage of the plan's soft cap (None without the cloud meter)."""
    meter = getattr(state, "usage_meter", None)
    if meter is None:
        return None
    try:
        account = await meter.get_account(user_id)
        counters = await meter.get_period_counters(account.pool, CreditPeriod.monthly(now).start)
        cap = int(account.limits.max_relay_events_per_month or 0)
        if cap <= 0:
            return None
        return math.floor(100 * int(counters.get("relay_events") or 0) / cap)
    except Exception as e:  # the board is best effort on the meter; its calls don't depend on it
        log.warning("marshal_board_meter_unavailable", error_type=type(e).__name__)
        return None


def model_state(settings: Any, snap: budget.Snapshot | None) -> dict[str, Any]:
    name = settings.marshal_model
    reason = offline_reason(settings)
    if reason == "breaker_open":
        retry = desk_breaker(settings).retry_after()
        return {"state": "offline", "reason": reason, "name": name, "retry_after_seconds": round(retry) if retry else None}
    if reason is not None:
        return {"state": "offline", "reason": reason, "name": name, "retry_after_seconds": None}
    day_cap, month_cap = budget.caps(settings)
    if snap is None or snap.day_used_micro + ASK_RESERVE_MICRO > day_cap:
        # An unreadable ledger refuses asks ("off for today"), exactly as a spent day does.
        return {"state": "offline", "reason": "daily_budget", "name": name, "retry_after_seconds": None}
    if snap.month_used_micro + ASK_RESERVE_MICRO > month_cap:
        return {"state": "offline", "reason": "monthly_budget", "name": name, "retry_after_seconds": None}
    if snap.user_asks >= int(settings.marshal_user_daily_asks):
        return {"state": "limited", "reason": "daily_asks", "name": name, "retry_after_seconds": None}
    return {"state": "ready", "reason": None, "name": name, "retry_after_seconds": None}


async def build_board(state: Any, user: AuthenticatedUser, now: datetime, settings: Any) -> dict[str, Any]:
    account = await read_account(state, user, now)
    seen = agents_seen(account.summary, account.trail)
    calls: list[dict[str, Any]] = []
    account_call: dict[str, Any] | None = None
    for agent in seen or ["claude-code"]:
        verdict = diagnose_agent(await diagnosis_input_for(state, user, agent, now, account))
        if verdict.code == "HANDED_OFF":
            continue
        if verdict.code in ACCOUNT_CODES:
            account_call = account_call or _call(verdict, None)  # one call for the account, not one per agent
            continue
        if seen:
            calls.append(_call(verdict, agent))
    ranked = rank(([account_call] if account_call else []) + calls)
    inbox = getattr(state, "inbox_manager", None)
    unread = int((await inbox.summary(user.user_id))["unread_total"]) if inbox is not None else 0
    fair_use = await _fair_use(state, user.user_id, now)
    try:
        snap: budget.Snapshot | None = await budget.snapshot(state.db, user.user_id, now)
    except Exception as e:
        log.warning("marshal_board_budget_unavailable", error_type=type(e).__name__)
        snap = None
    shown = ranked[:MAX_CALLS]
    return {
        "status_line": status_line(ranked, unread, fair_use, bool(seen)),
        "calls": shown,
        "suggestions": suggestions(shown, account.trail, unread=unread, fair_use=fair_use),
        "model": model_state(settings, snap),
        "asks": {
            "used": snap.user_asks if snap else 0,
            "limit": int(settings.marshal_user_daily_asks),
            "resets_at": budget.iso_z(budget.next_utc_midnight(now)),
        },
        "footer": FOOTER,
        "generated_at": budget.iso_z(now),
    }
