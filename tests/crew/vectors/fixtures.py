"""Factories for crew contract vectors.

Only *inputs* are built here (views, envelopes, snapshots). Every expected
value in the vector files is written out literally in ``build.py`` and
``build_corpora.py``; nothing is computed by the implementations under test.
"""

from __future__ import annotations

import hashlib
from typing import Any

from remembra.crew.schemas import COLLISION_SEVERITY, is_moment

CREW = "crw_0a1b2c3d4e5f6a7b"
OTHER_CREW = "crw_ffffffffffffffff"
PROJECT = "yaadbooks"
USER = "u_mani"


def ts(sec: int) -> str:
    """2026-09-25T20:00:00Z plus ``sec`` seconds."""
    return f"2026-09-25T{20 + sec // 3600:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}.000Z"


def _hex8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def crew(**kw: Any) -> dict[str, Any]:
    base = {
        "id": CREW,
        "project_id": PROJECT,
        "name": "YaadBooks",
        "mode": "multi",
        "enforcement": "enforce",
        "settings_version": 1,
        "last_seq": 10,
    }
    base.update(kw)
    return base


def session(sid: str, callsign: str, agent: str = "claude-code", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": sid,
        "callsign": callsign,
        "agent_id": agent,
        "member_key": f"{agent}:mbp:{_hex8(sid)}",
        "agent_verified": True,
        "adapter": agent,
        "adapter_enforcement": "enforced" if agent == "claude-code" else "advisory",
        "client_kind": "hook",
        "model": None,
        "host_id": "hst_mbp",
        "state": "active",
        "quiet_reason": None,
        "state_reason": None,
        "stuck": False,
        "branch": f"feat/{callsign}",
        "head_commit": None,
        "worktree_id": f"wt-{callsign}",
        "githook_state": "ok",
        "current_task_id": None,
        "limit": None,
        "joined_at": ts(0),
        "last_activity_at": ts(0),
        "ended_at": None,
        "end_reason": None,
    }
    base.update(kw)
    return base


def zone(zid: str, slug: str, include: list[str], **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": zid,
        "slug": slug,
        "title": slug.upper() + " section",
        "parent_id": None,
        "is_leaf": True,
        "builtin": False,
        "include_globs": include,
        "exclude_globs": [],
        "services": [],
        "command_patterns": [],
        "mcp_tools": [],
        "mode": "exclusive",
        "auto_claim": True,
        "protected": False,
        "reserve_for": None,
        "fail_closed": False,
        "frozen_by": None,
        "frozen_note": None,
        "frozen_until": None,
        "source": "repo",
        "version": 1,
    }
    base.update(kw)
    return base


def claim(cid: str, holder: str | None, state: str = "active", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": cid,
        "zone_id": None,
        "path_glob": None,
        "resource": None,
        "mode": "exclusive",
        "holder_kind": "session" if holder else "human",
        "holder_session_id": holder,
        "holder_agent_id": "claude-code" if holder else None,
        "holder_user_id": USER,
        "task_id": None,
        "state": state,
        "source": "first_write",
        "epoch": 1,
        "unconfirmed": False,
        "fenced": False,
        "lease_expires_at": ts(600) if state == "active" else None,
        "reserve_reason": None,
        "reserved_for": None,
        "offered_to": None,
        "queue_pos": None,
        "baton_ref": None,
        "granted_at": ts(0) if state in ("active", "reserved", "offered") else None,
        "version": 1,
    }
    base.update(kw)
    return base


def task(tid: str, number: int, status: str = "ready", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": tid,
        "number": number,
        "title": f"Task {number}",
        "status": status,
        "status_before_stall": None,
        "phase": None,
        "priority": 2,
        "zone_ids": [],
        "owner_session_id": None,
        "owner_agent_id": None,
        "reviewer": None,
        "depends_on": [],
        "acceptance": [],
        "acceptance_locked": status not in ("backlog", "ready", "claimed"),
        "started_head": None,
        "current_report_id": None,
        "blocked_reason": None,
        "version": 1,
    }
    base.update(kw)
    return base


def collision(cid: str, kind: str, state: str = "open", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": cid,
        "kind": kind,
        "severity": COLLISION_SEVERITY[kind],
        "subject": "src/app/pos/cart.ts",
        "zone_id": "zn_pos",
        "session_a": "cs_a",
        "session_b": "cs_b",
        "claim_id": None,
        "attribution": "certain",
        "state": state,
        "escalated": False,
        "resolution": None,
    }
    base.update(kw)
    return base


def decision(did: str, number: int, state: str, **kw: Any) -> dict[str, Any]:
    human = state == "in_force"
    base: dict[str, Any] = {
        "id": did,
        "number": number,
        "title": f"Decision {number}",
        "decision": "GCT rounding half-up per line",
        "state": state,
        "source": "direct",
        "decided_by_kind": "human" if human else "agent",
        "decided_by": USER if human else "cs_a",
        "confirmed_by": USER if human else None,
        "task_id": None,
        "zone_id": None,
        "supersedes_id": None,
    }
    base.update(kw)
    return base


def message(mid: str, seq: int, body: str = "hello", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": mid,
        "seq": seq,
        "thread_root_id": None,
        "reply_to_id": None,
        "kind": "chat",
        "author_kind": "agent",
        "author_session_id": "cs_a",
        "author_agent_id": "claude-code",
        "author_verified": True,
        "body": body,
        "body_truncated": False,
        "mentions": [],
        "refs": [],
        "edited": False,
        "redacted": False,
        "pinned": False,
    }
    base.update(kw)
    return base


def inbox_item(iid: str, audience: str, state: str = "open", kind: str = "review_report", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": iid,
        "audience": audience,
        "recipient": None,
        "kind": kind,
        "origin": "server",
        "ref_type": None,
        "ref_id": None,
        "priority": 2,
        "title": "Review T-14",
        "primary_action": None,
        "state": state,
        "claimed_by": None,
        "coalesced_count": 1,
    }
    base.update(kw)
    return base


def report(rid: str, task_id: str, kind: str, is_current: bool = True, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": rid,
        "task_id": task_id,
        "session_id": "cs_a",
        "kind": kind,
        "verdict": "complete" if kind == "completion" else ("partial" if kind == "partial" else None),
        "review_state": None,
        "is_current": is_current,
        "superseded_reason": None,
        "facts_source": "relay-cli",
        "criteria": [],
        "baton_ref": None,
        "handoff_id": None,
    }
    base.update(kw)
    return base


SESSION_ACTORS = {
    "cs_a": ("cc-1", "claude-code"),
    "cs_b": ("codex-1", "codex"),
    "cs_c": ("cc-2", "claude-code"),
    "cs_p": ("cc-3", "claude-code"),
}


def actor(kind: str, ident: str) -> dict[str, Any]:
    if kind == "session":
        callsign, agent = SESSION_ACTORS.get(ident, ("cc-9", "claude-code"))
        return {"kind": "session", "id": ident, "callsign": callsign, "agent_id": agent, "user_id": USER, "verified": True}
    if kind == "human":
        return {"kind": "human", "id": ident, "callsign": None, "agent_id": None, "user_id": ident, "verified": True}
    return {"kind": "system", "id": ident, "callsign": None, "agent_id": None, "user_id": None, "verified": True}


def event(
    seq: int,
    type_: str,
    payload: dict[str, Any],
    *,
    by: tuple[str, str] = ("system", "server"),
    refs: dict[str, Any] | None = None,
    origin: str = "server",
    crew_id: str = CREW,
    severity: str = "info",
    v: int = 1,
) -> dict[str, Any]:
    act = actor(*by)
    ref = dict(refs or {})
    if by[0] == "session":
        ref.setdefault("session_id", by[1])
    return {
        "seq": seq,
        "id": f"evt_{seq:08d}",
        "crew_id": crew_id,
        "project_id": PROJECT,
        "ts": ts(seq),
        "type": type_,
        "v": v,
        "origin": origin,
        "actor": act,
        "refs": ref,
        "severity": severity,
        "moment": is_moment(type_, payload, act["kind"]),
        "summary": f"{type_} #{seq}",
        "payload": payload,
    }


def frame(ev: dict[str, Any]) -> dict[str, Any]:
    return {"type": "crew.event", "crew_id": ev["crew_id"], "data": ev}


def base_snapshot(**kw: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "crew": crew(),
        "server_time": ts(10),
        "as_of_seq": 10,
        "etag": '"10"',
        "sessions": [
            session("cs_a", "cc-1", current_task_id="tsk_14"),
            session("cs_b", "codex-1", agent="codex"),
        ],
        "claims": [
            claim("clm_pos", "cs_a", zone_id="zn_pos", task_id="tsk_14", source="task"),
            claim("clm_old", "cs_b", state="released", zone_id="zn_reports"),
        ],
        "zones": [
            zone("zn_pos", "pos", ["src/app/pos/**"]),
            zone("zn_reports", "reports", ["src/app/reports/**"]),
        ],
        "commons": [{"glob": "package.json", "kind": "plain"}, {"glob": "package-lock.json", "kind": "serialize"}],
        "ignore": ["docs/**"],
        "tasks": [
            task("tsk_14", 14, "in_progress", zone_ids=["zn_pos"], owner_session_id="cs_a", owner_agent_id="claude-code"),
            task("tsk_9", 9, "ready"),
        ],
        "collisions": [
            collision("col_1", "same_file"),
            collision("col_2", "same_file", state="resolved"),
        ],
        "decisions": [
            decision("dec_1", 1, "in_force"),
            decision("dec_2", 2, "proposed"),
            decision("dec_3", 3, "rejected"),
        ],
        "offers": [],
        "footprints": [],
        "inbox_counts": {"project": 2, "crew": 1},
        "pending_zone_changes": ["zch_1"],
    }
    snap.update(kw)
    return snap
