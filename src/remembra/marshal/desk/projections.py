"""What the model sees of each read, and the one-line summary the dashboard shows for it.

A projection keeps only the fields listed per tool (plan 3.3): no rendered
brief, no handoff detail, no trust scores, no key material, no daily series.
Every timestamp gets a sibling ``<field>_ago`` ("2h ago", the dashboard's
wording), so the model never computes a time. Lists are cut to their limits,
strings to theirs, and a size clamp drops list items from the end until the
JSON fits in ``TOOL_RESULT_MAX_CHARS`` (then ``truncated: true``).

Summaries are deterministic, built from the cleaned projection by rules: the
read lines and the fallback answer are never written by the model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from remembra.marshal.desk.constants import TOOL_RESULT_MAX_CHARS
from remembra.marshal.diagnosis import relative_time
from remembra.marshal.words import plural
from remembra.relay.handoff import handoff_headline

HEADLINE_CHARS = 160
BRIEF_STRING_CHARS = 240
DOC_SECTION_CHARS = 1200
INBOX_NOT_CHECKED = "No agent_id: inbox not checked"


def clip(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _with_ago(row: Mapping[str, Any], fields: tuple[str, ...], now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value
        if key in fields:
            out[f"{key}_ago"] = relative_time(value, now) if value else None
    return out


def _pick(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    source = row if isinstance(row, Mapping) else {}
    return {key: source.get(key) for key in keys}


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _lists(value: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(value, list):
        if value:
            found.append(value)
        for item in value:
            found.extend(_lists(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_lists(item))
    return found


def _longest_string(value: Any, best: tuple[int, Any, Any] | None = None) -> tuple[int, Any, Any] | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (best is None or len(item) > best[0]):
                best = (len(item), value, key)
            else:
                best = _longest_string(item, best)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str) and (best is None or len(item) > best[0]):
                best = (len(item), value, index)
            else:
                best = _longest_string(item, best)
    return best


def clamp_size(payload: dict[str, Any], limit: int = TOOL_RESULT_MAX_CHARS) -> dict[str, Any]:
    """Drop list items from the end (the largest list first) until the JSON fits; mark it ``truncated``."""
    if len(_dump(payload)) <= limit:
        return payload
    payload["truncated"] = True
    while len(_dump(payload)) > limit:
        lists = _lists(payload)
        if lists:
            max(lists, key=lambda items: len(_dump(items))).pop()
            continue
        longest = _longest_string(payload)
        if longest is None or longest[0] <= 1:
            break
        _size, holder, key = longest
        holder[key] = clip(holder[key], max(1, longest[0] // 2))
    return payload


# ---------------------------------------------------------------------------
# trail_summary
# ---------------------------------------------------------------------------


def project_trail_summary(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    week = data.get("week") or {}
    agents = [
        _with_ago(
            {
                **_pick(a, ("agent_id", "handoffs", "checkpoints", "last_active")),
                "sessions_7d": (a or {}).get("sessions_7d"),
                "projects": list((a or {}).get("projects") or [])[:10],
            },
            ("last_active",),
            now,
        )
        for a in list(data.get("agents") or [])[:12]
    ]
    projects = [
        _with_ago(
            {
                **_pick(p, ("project_id", "handoffs", "checkpoints", "last_active")),
                "agents": list((p or {}).get("agents") or [])[:10],
            },
            ("last_active",),
            now,
        )
        for p in list(data.get("projects") or [])[:12]
    ]
    return clamp_size(
        {
            "days": data.get("days"),
            "total_handoffs": data.get("total_handoffs"),
            "total_checkpoints": data.get("total_checkpoints"),
            "week": {
                "handoffs": week.get("handoffs"),
                "checkpoints": week.get("checkpoints"),
                "agents": list(week.get("agents") or [])[:12],
                "projects": list(week.get("projects") or [])[:12],
            },
            "agents": agents,
            "projects": projects,
        }
    )


def summary_trail_summary(p: Mapping[str, Any]) -> str:
    days = _int(p.get("days"))
    agents = list(p.get("agents") or [])
    if not agents:
        total = _int(p.get("total_handoffs")) + _int(p.get("total_checkpoints"))
        if not total:
            return f"no entries · {days}d"
        return f"{plural(total, 'entry', 'entries')} · no agent ids · {days}d"
    parts = [plural(len(agents), "agent")]
    parts += [f"{a.get('agent_id')} {plural(_int(a.get('handoffs')), 'handoff')}" for a in agents[:3]]
    parts += [f"newest {agents[0].get('last_active_ago') or 'unknown time'}", f"{days}d"]
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# trail
# ---------------------------------------------------------------------------


def _trail_item(item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    trust = item.get("trust") or {}
    withheld = bool(trust.get("withheld"))
    health = item.get("health")
    row: dict[str, Any] = {
        **_pick(item, ("id", "project_id", "memory_type", "agent_id", "created_at")),
        "created_at_ago": relative_time(item.get("created_at"), now) if item.get("created_at") else None,
        "branch": item.get("branch"),
    }
    if not withheld:
        row["headline"] = clip(item.get("headline"), HEADLINE_CHARS)
    row.update(
        {
            "withheld": withheld,
            "flags": list(trust.get("flags") or []),
            "failing": item.get("failing"),
            "open": item.get("open"),
            "health": _pick(health, ("status", "label")) if isinstance(health, Mapping) else None,
            "picked_up_by": [
                _with_ago(_pick(p, ("agent_id", "agent_verified", "picked_up_at")), ("picked_up_at",), now)
                for p in list(item.get("picked_up_by") or [])[:10]
            ],
        }
    )
    return row


def project_trail(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    items = [_trail_item(item, now) for item in list(data.get("items") or [])[:20]]
    return clamp_size({"total": data.get("total"), "items": items})


def summary_trail(p: Mapping[str, Any], args: Mapping[str, Any]) -> str:
    items = list(p.get("items") or [])
    if not items:
        return "no entries"
    who = args.get("agent_id") or args.get("project_id")
    observed = {item.get(k) for item in items for k in ("agent_id", "project_id")}
    prefix = f"{who}: " if who in observed else ""
    total = _int(p.get("total")) or len(items)
    return f"{prefix}{plural(total, 'entry', 'entries')} · newest {items[0].get('created_at_ago') or 'unknown time'}"


# ---------------------------------------------------------------------------
# brief_preview
# ---------------------------------------------------------------------------


def _strings(values: Any, limit: int) -> list[Any]:
    return [clip(v, BRIEF_STRING_CHARS) for v in list(values or [])[:limit]]


def project_brief(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    handoff = data.get("handoff")
    projected_handoff: dict[str, Any] | None = None
    if isinstance(handoff, Mapping):
        meta: Mapping[str, Any] = handoff["metadata"] if isinstance(handoff.get("metadata"), Mapping) else {}
        relay: Mapping[str, Any] = meta["relay"] if isinstance(meta.get("relay"), Mapping) else {}
        withheld = bool(handoff.get("withheld"))
        projected_handoff = {
            **_pick(handoff, ("id", "agent_id", "created_at")),
            "created_at_ago": relative_time(handoff.get("created_at"), now) if handoff.get("created_at") else None,
            "withheld": withheld,
            "flags": list(handoff.get("flags") or []),
        }
        if not withheld:
            projected_handoff.update(
                {
                    "headline": clip(handoff_headline(dict(handoff)), BRIEF_STRING_CHARS),
                    "done": _strings(relay.get("done"), 5),
                    "not_done": _strings(relay.get("not_done"), 5),
                    "failing": _strings(relay.get("failing"), 5),
                    "next": clip(relay.get("next"), BRIEF_STRING_CHARS),
                }
            )
    health = data.get("handoff_health")
    recent = []
    for mem in list(data.get("recent") or [])[:5]:
        row = {
            **_pick(mem, ("id", "agent_id", "memory_type", "created_at")),
            "created_at_ago": relative_time(mem.get("created_at"), now) if mem.get("created_at") else None,
        }
        if not mem.get("withheld"):
            row["headline"] = clip(handoff_headline(dict(mem)), BRIEF_STRING_CHARS)
        recent.append(row)
    warnings = [w for w in data.get("warnings") or [] if not str(w).startswith(INBOX_NOT_CHECKED)]
    return clamp_size(
        {
            "project_id": data.get("project_id"),
            "handoff": projected_handoff,
            "handoffs_skipped": data.get("handoffs_skipped"),
            "handoff_health": {
                "status": health.get("status"),
                "label": health.get("label"),
                "missing": _strings(health.get("missing"), 5),
            }
            if isinstance(health, Mapping)
            else None,
            "warnings": _strings(warnings, 6),
            "recent": recent,
            "linked_projects": [_pick(link, ("project_id", "relation")) for link in list(data.get("linked_projects") or [])[:5]],
        }
    )


def summary_brief(p: Mapping[str, Any], args: Mapping[str, Any]) -> str:
    project = p.get("project_id")
    handoff = p.get("handoff")
    if not isinstance(handoff, Mapping):
        return "no handoff yet"
    warnings = plural(len(p.get("warnings") or []), "warning")
    ago = handoff.get("created_at_ago") or "at an unknown time"
    return f"{project} · last handoff by {handoff.get('agent_id')} {ago} · {warnings}"


# ---------------------------------------------------------------------------
# inbox_summary, usage_summary, usage_daily, plan
# ---------------------------------------------------------------------------


def project_inbox(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    agents = [
        _with_ago(_pick(a, ("agent_id", "unread", "open", "received", "sent", "last_at")), ("last_at",), now)
        for a in list(data.get("agents") or [])[:12]
    ]
    return clamp_size({"unread_total": data.get("unread_total"), "open_total": data.get("open_total"), "agents": agents})


def summary_inbox(p: Mapping[str, Any]) -> str:
    return f"{_int(p.get('unread_total'))} unread · {_int(p.get('open_total'))} open"


def project_usage_summary(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return clamp_size(
        {
            **_pick(data, ("plan", "plan_name", "interval")),
            "period": _pick(data.get("period"), ("start", "end")),
            "credits": _pick(data.get("credits"), ("limit", "used", "remaining", "bank")),
            "enrichment": _pick(data.get("enrichment"), ("status", "reason")),
            "relay_events": _pick(data.get("relay_events"), ("this_month", "soft_cap", "over_soft_cap")),
            "recalls": _pick(data.get("recalls"), ("this_month", "limit")),
            "memories": _pick(data.get("memories"), ("stored", "cap", "handoffs")),
        }
    )


def summary_usage_summary(p: Mapping[str, Any]) -> str:
    relay = p.get("relay_events") or {}
    credits = p.get("credits") or {}
    return (
        f"{p.get('plan')} · relay {_int(relay.get('this_month'))} of {_int(relay.get('soft_cap'))} events this month"
        f" · credits {_int(credits.get('used'))} of {_int(credits.get('limit'))} used"
    )


def project_usage_daily(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    days = []
    for row in list(data.get("days") or [])[:30]:
        if not isinstance(row, Mapping):
            continue
        kept: dict[str, Any] = {"date": row.get("date")}
        kept.update({k: v for k, v in row.items() if isinstance(v, int) and not isinstance(v, bool)})
        days.append(kept)
    return clamp_size({"days": days})


def summary_usage_daily(p: Mapping[str, Any]) -> str:
    days = list(p.get("days") or [])
    stores = sum(_int(d.get("stores")) for d in days)
    recalls = sum(_int(d.get("recalls")) for d in days)
    return f"{plural(len(days), 'day')} · {plural(stores, 'store')} · {plural(recalls, 'recall')}"


def project_plan(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    limits = data.get("limits") or {}
    max_projects = limits.get("max_projects")
    checks = data.get("limit_checks") or {}
    usage: Mapping[str, Any] = data["usage"] if isinstance(data.get("usage"), Mapping) else {}
    return clamp_size(
        {
            "plan": data.get("plan"),
            "limits": {
                **_pick(limits, ("max_api_keys", "max_memories", "max_recalls_per_month", "relay_events_soft_cap")),
                "max_projects": "unlimited" if max_projects == 1000 else max_projects,
            },
            "usage": {k: v for k, v in usage.items() if isinstance(v, int | float | str) or v is None},
            "limit_checks": {name: _pick(checks.get(name), ("allowed", "reason")) for name in ("store", "recall", "create_key")},
        }
    )


def summary_plan(p: Mapping[str, Any]) -> str:
    usage = p.get("usage") or {}
    limits = p.get("limits") or {}
    create_key = (p.get("limit_checks") or {}).get("create_key") or {}
    allowed = "allowed" if create_key.get("allowed") else "refused"
    return f"{p.get('plan')} · keys {_int(usage.get('api_keys_active'))} of {limits.get('max_api_keys')} · create_key {allowed}"


# ---------------------------------------------------------------------------
# diagnose_agent
# ---------------------------------------------------------------------------


def project_diagnosis(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    verdict = data.get("verdict") or {}
    fix = verdict.get("fix")
    evidence = data.get("evidence") or {}
    keys = evidence.get("keys") or {}
    entries = evidence.get("entries") or {}
    return clamp_size(
        {
            "agent_id": data.get("agent_id"),
            "verdict": {
                **_pick(verdict, ("code", "proven", "verdict", "detail")),
                "causes": list(verdict.get("causes") or []),
                "unverified": verdict.get("unverified"),
                "fix": {
                    "text": fix.get("text"),
                    "commands": [_pick(c, ("prompt", "text")) for c in fix.get("commands") or []],
                }
                if isinstance(fix, Mapping)
                else None,
                "then": verdict.get("then"),
                "commands": list(verdict.get("commands") or []),
                "caveat": verdict.get("caveat"),
                "doc": verdict.get("doc"),
                "lines": [_pick(line, ("label", "text")) for line in verdict.get("lines") or []],
            },
            "evidence": {
                "keys": _with_ago(_pick(keys, ("active", "used", "newest_used_at", "newest_name")), ("newest_used_at",), now),
                "entries": _with_ago(_pick(entries, ("count", "handoffs", "newest_at")), ("newest_at",), now),
                "pickups": _pick(evidence.get("pickups"), ("briefs", "others_handoffs", "trail_entries")),
            },
        }
    )


def summary_diagnosis(p: Mapping[str, Any]) -> str:
    verdict = p.get("verdict") or {}
    return f"{p.get('agent_id')} · {verdict.get('code')} · {'proven' if verdict.get('proven') else 'inferred'}"


# ---------------------------------------------------------------------------
# docs_lookup
# ---------------------------------------------------------------------------


def project_docs(data: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return clamp_size(
        {
            "answer_status": data.get("answer_status"),
            "sections": [
                {"title": s.get("title"), "url": s.get("url"), "text": clip(s.get("text"), DOC_SECTION_CHARS)}
                for s in list(data.get("sections") or [])[:3]
            ],
            "pages": list(data.get("pages") or []),
            "facts": list(data.get("facts") or []),
        }
    )


def summary_docs(p: Mapping[str, Any]) -> str:
    sections = list(p.get("sections") or [])
    if sections:
        return f"{plural(len(sections), 'section')} · {sections[0].get('url')}"
    status = p.get("answer_status")
    if status == "read_the_page" and p.get("pages"):
        return f"read the page · {p['pages'][0]}"
    if status == "answered" and p.get("facts"):
        return f"{plural(len(p['facts']), 'fact')} · from the package"
    return "can't confirm"
