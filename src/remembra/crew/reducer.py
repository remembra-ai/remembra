"""The shared crew reducer (§4.4), Python reference implementation.

One reducer spec (``docs/crew/reducer.md``) with test vectors
(``tests/crew/vectors/reducer/*.json``), implemented here for crewd and
``remembra-crew watch`` and in TypeScript for the dashboard (WP-12). Both must
satisfy the same vectors.

Stdlib only. The state is plain JSON (dicts, lists, str, int, bool, None) so
the Python and TypeScript implementations can be compared value for value.

Rules in one paragraph: the state starts from a snapshot; events are applied
strictly by ``seq``; a duplicate (``seq <= last_seq``) is ignored; a gap
(``seq > last_seq + 1``) or a ``resync_required`` frame sets ``needs_resync``
and every later event is ignored until a new snapshot frame arrives; events of
another crew are ignored; an event type the reducer does not know (a newer
server) only advances ``last_seq``. Presence frames never change ``state``:
they replace the ephemeral ``presence`` overlay of a known session.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, Final

from remembra.crew.schemas import (
    LIVE_CLAIM_STATES,
    LIVE_COLLISION_STATES,
    LIVE_DECISION_STATES,
    LIVE_INBOX_STATES,
)

State = dict[str, Any]

MOMENTS_KEEP: Final = 50
MESSAGES_KEEP: Final = 100
BATONS_KEEP: Final = 20
BATON_REFS_KEEP: Final = 50
COUNTED_AUDIENCES: Final = ("project", "crew")


def empty_state() -> State:
    return {
        "crew": None,
        "mode": "solo",
        "last_seq": 0,
        "needs_resync": False,
        "resync_reason": None,
        "sessions": {},
        "claims": {},
        "zones": {},
        "tasks": {},
        "collisions": {},
        "decisions": {},
        "offers": {},
        "inbox": {},
        "inbox_counts": {"project": 0, "crew": 0},
        "reports": {},
        "checkpoints": {},
        "hosts": {},
        "pending_zone_changes": {},
        "messages": [],
        "moments": [],
        "batons": [],
        "baton_refs": {},
        "guard_blocks": {},
        "tamper_blocks": {},
        "budget": {},
    }


def from_snapshot(snapshot: Mapping[str, Any]) -> State:
    """Initial state from ``GET /crews/{id}/snapshot`` (or a crewd local snapshot)."""
    state = empty_state()
    crew = copy.deepcopy(dict(snapshot["crew"]))
    state["crew"] = crew
    state["mode"] = crew.get("mode", "solo")
    state["last_seq"] = int(snapshot["as_of_seq"])
    for s in snapshot.get("sessions", []):
        state["sessions"][s["id"]] = {**copy.deepcopy(s), "presence": None}
    for c in snapshot.get("claims", []):
        if c["state"] in LIVE_CLAIM_STATES:
            state["claims"][c["id"]] = copy.deepcopy(c)
    for z in snapshot.get("zones", []):
        state["zones"][z["id"]] = copy.deepcopy(z)
    for t in snapshot.get("tasks", []):
        state["tasks"][t["id"]] = copy.deepcopy(t)
    for c in snapshot.get("collisions", []):
        if c["state"] in LIVE_COLLISION_STATES:
            state["collisions"][c["id"]] = copy.deepcopy(c)
    for d in snapshot.get("decisions", []):
        if d["state"] in LIVE_DECISION_STATES:
            state["decisions"][d["id"]] = copy.deepcopy(d)
    for o in snapshot.get("offers", []):
        state["offers"][o["id"]] = copy.deepcopy(o)
    counts = snapshot.get("inbox_counts") or {}
    state["inbox_counts"] = {"project": int(counts.get("project", 0)), "crew": int(counts.get("crew", 0))}
    for change_id in snapshot.get("pending_zone_changes", []):
        state["pending_zone_changes"][change_id] = {"loosening": None}
    return state


def reduce(snapshot: Mapping[str, Any], frames: Iterable[Mapping[str, Any]]) -> State:
    state = from_snapshot(snapshot)
    for frame in frames:
        state = apply_frame(state, frame)
    return state


def apply_frame(state: State, frame: Mapping[str, Any]) -> State:
    """Apply one WebSocket/polling frame. Mutates and returns ``state`` (a snapshot frame returns a new one)."""
    kind = frame.get("type")
    if kind == "snapshot":
        return from_snapshot(frame["data"])
    if kind == "crew.event":
        return apply_event(state, frame["data"])
    if kind == "presence":
        return apply_presence(state, frame)
    if kind == "resync_required":
        if _crew_matches(state, frame.get("crew_id")):
            state["needs_resync"] = True
            state["resync_reason"] = "server"
        return state
    return state  # crew.subscribed, crew.summary and unknown frames do not change crew state


def apply_presence(state: State, frame: Mapping[str, Any]) -> State:
    if not _crew_matches(state, frame.get("crew_id")):
        return state
    for lane in frame.get("lanes", []):
        session = state["sessions"].get(lane.get("session_id"))
        if session is None:
            continue
        session["presence"] = {k: copy.deepcopy(v) for k, v in lane.items() if k != "session_id"}
    return state


def apply_event(state: State, event: Mapping[str, Any]) -> State:
    if not _crew_matches(state, event.get("crew_id")):
        return state
    if state["needs_resync"]:
        return state
    seq = int(event["seq"])
    if seq <= state["last_seq"]:
        return state  # duplicate or replay overlap
    if seq > state["last_seq"] + 1:
        state["needs_resync"] = True
        state["resync_reason"] = "gap"
        return state
    handler = _HANDLERS.get(str(event.get("type")))
    if handler is not None and int(event.get("v", 1)) == 1:
        handler(state, event, event.get("payload") or {})
    state["last_seq"] = seq
    if state["crew"] is not None:
        state["crew"]["last_seq"] = seq
    if event.get("moment"):
        state["moments"].append({"seq": seq, "type": event["type"], "summary": event.get("summary", ""), "ts": event.get("ts")})
        del state["moments"][:-MOMENTS_KEEP]
    return state


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _crew_matches(state: State, crew_id: Any) -> bool:
    crew = state.get("crew")
    return crew is None or crew.get("id") == crew_id


def _sid(event: Mapping[str, Any]) -> str | None:
    refs = event.get("refs") or {}
    if refs.get("session_id"):
        return str(refs["session_id"])
    actor = event.get("actor") or {}
    return str(actor["id"]) if actor.get("kind") == "session" and actor.get("id") else None


def _session(state: State, event: Mapping[str, Any]) -> dict[str, Any] | None:
    sid = _sid(event)
    return state["sessions"].get(sid) if sid else None


def _crew_created(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    state["crew"] = copy.deepcopy(dict(p["crew"]))
    state["mode"] = state["crew"]["mode"]


def _crew_settings(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    if state["crew"] is not None:
        state["crew"]["settings_version"] = p["settings_version"]
        if p.get("enforcement"):
            state["crew"]["enforcement"] = p["enforcement"]


def _crew_mode(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    state["mode"] = p["to"]
    if state["crew"] is not None:
        state["crew"]["mode"] = p["to"]


def _host(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    kind = event["type"]
    if kind == "host.registered":
        host = p["host"]
        state["hosts"][host["id"]] = {"id": host["id"], "state": host["state"]}
        return
    new_state = "unreachable" if kind == "host.unreachable" else "online"
    state["hosts"][p["host_id"]] = {"id": p["host_id"], "state": new_state}


def _session_joined(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    s = p["session"]
    state["sessions"][s["id"]] = {**copy.deepcopy(dict(s)), "presence": None}


def _session_change(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    s = _session(state, event)
    if s is None:
        return
    kind = event["type"]
    if kind == "session.state_changed":
        s["state"] = p["to"]
        s["quiet_reason"] = p.get("quiet_reason") if p["to"] == "quiet" else None
        s["state_reason"] = p["reason"]
    elif kind == "session.recovered":
        s["state"] = "active"
        s["quiet_reason"] = None
        s["state_reason"] = None
    elif kind == "session.quota_blocked":
        s["state"] = "quota_blocked"
        s["state_reason"] = p["error"]
    elif kind == "session.limit_warning":
        s["limit"] = {"level": p["level"], "pct": p.get("pct"), "source": p["source"]}
    elif kind == "session.stuck":
        s["stuck"] = bool(p["stuck"])
    elif kind == "session.paused":
        s["state"] = "paused"
    elif kind == "session.resumed":
        s["state"] = p["to"]
    elif kind == "session.left":
        s["state"] = "ended"
        s["end_reason"] = p["reason"]
        s["ended_at"] = event.get("ts")
        s["presence"] = None
    elif kind == "session.lost":
        s["state"] = "lost"
        s["state_reason"] = p["reason"]
        s["presence"] = None


def _activity(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    s = _session(state, event)
    if s is None:
        return
    s["last_activity_at"] = event.get("ts")
    if event["type"] == "activity.commit":
        s["head_commit"] = p["sha"]


def _zone(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    zone = p["zone"]
    if event["type"] == "zone.archived":
        state["zones"].pop(zone["id"], None)
    else:
        state["zones"][zone["id"]] = copy.deepcopy(dict(zone))


def _zone_change(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    if event["type"] == "zone.change_pending":
        state["pending_zone_changes"][p["change_id"]] = {"loosening": bool(p["loosening"])}
    else:
        state["pending_zone_changes"].pop(p["change_id"], None)


def _claim(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    claim = p.get("claim")
    if not claim:
        return
    cid = claim["id"]
    if claim["state"] in LIVE_CLAIM_STATES:
        state["claims"][cid] = copy.deepcopy(dict(claim))
    else:
        state["claims"].pop(cid, None)
    if claim["state"] != "reserved":
        for oid in [oid for oid, o in state["offers"].items() if o["claim_id"] == cid]:
            del state["offers"][oid]


def _claim_offered(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    offer = p["offer"]
    state["offers"][offer["id"]] = copy.deepcopy(dict(offer))


def _claim_fenced(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    claim = state["claims"].get(p["claim_id"])
    if claim is not None:
        claim["fenced"] = True


def _baton_passed(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    state["batons"].append({"seq": event["seq"], "ts": event.get("ts"), **copy.deepcopy(dict(p))})
    del state["batons"][:-BATONS_KEEP]


def _baton_ref(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    refs = state["baton_refs"]
    refs[p["ref"]] = {
        "seq": event["seq"],
        "task_id": p.get("task_id"),
        "session_id": _sid(event),
        "dirty_files": p["dirty_files"],
        "unpushed": p["unpushed"],
    }
    while len(refs) > BATON_REFS_KEEP:
        oldest = min(refs, key=lambda r: refs[r]["seq"])
        del refs[oldest]


def _guard(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    sid = _sid(event)
    if sid is None:
        return
    if event["type"] == "guard.blocked":
        state["guard_blocks"][sid] = state["guard_blocks"].get(sid, 0) + int(p.get("coalesced", 1))
    else:
        state["tamper_blocks"][sid] = state["tamper_blocks"].get(sid, 0) + 1


def _githook(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    s = _session(state, event)
    if s is not None:
        s["githook_state"] = p["state"]


def _collision(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    c = p["collision"]
    if c["state"] in LIVE_COLLISION_STATES:
        state["collisions"][c["id"]] = copy.deepcopy(dict(c))
    else:
        state["collisions"].pop(c["id"], None)


def _task(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    t = p["task"]
    state["tasks"][t["id"]] = copy.deepcopy(dict(t))


def _checkpoint(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    c = p["checkpoint"]
    state["checkpoints"][c["session_id"]] = copy.deepcopy(dict(c))


def _report(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    r = p["report"]
    current = state["reports"].get(r["task_id"])
    if r["is_current"]:
        state["reports"][r["task_id"]] = copy.deepcopy(dict(r))
    elif current is not None and current["id"] == r["id"]:
        del state["reports"][r["task_id"]]


def _message(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    msgs: list[dict[str, Any]] = state["messages"]
    kind = event["type"]
    if kind == "message.posted":
        msgs.append(copy.deepcopy(dict(p["message"])))
        msgs.sort(key=lambda m: m["seq"])
        del msgs[:-MESSAGES_KEEP]
        return
    target = p["message"]["id"] if kind == "message.edited" else p["message_id"]
    for idx, m in enumerate(msgs):
        if m["id"] != target:
            continue
        if kind == "message.edited":
            msgs[idx] = copy.deepcopy(dict(p["message"]))
        else:
            m["body"] = ""
            m["body_truncated"] = False
            m["redacted"] = True
        return


def _decision(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    d = p["decision"]
    if d["state"] in LIVE_DECISION_STATES:
        state["decisions"][d["id"]] = copy.deepcopy(dict(d))
    else:
        state["decisions"].pop(d["id"], None)


def _inbox(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    item = p["item"]
    iid, audience = item["id"], item["audience"]
    counted = audience in COUNTED_AUDIENCES
    known = iid in state["inbox"]
    counts = state["inbox_counts"]
    if item["state"] in LIVE_INBOX_STATES:
        if not known and event["type"] == "inbox.item_created" and counted:
            counts[audience] += 1
        state["inbox"][iid] = copy.deepcopy(dict(item))
        return
    # resolved or dismissed: it was counted either here (known) or in the snapshot (not known)
    state["inbox"].pop(iid, None)
    if counted:
        counts[audience] = max(0, counts[audience] - 1)


def _budget(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    state["budget"][p["metric"]] = {"used": p["used"], "limit": p["limit"], "capped": event["type"] == "budget.cap_reached"}


def _noop(state: State, event: Mapping[str, Any], p: Mapping[str, Any]) -> None:
    return None


_HANDLERS: Final = {
    "crew.created": _crew_created,
    "crew.settings_changed": _crew_settings,
    "crew.mode_changed": _crew_mode,
    "crew.shift_started": _noop,
    "crew.shift_ended": _noop,
    "host.registered": _host,
    "host.unreachable": _host,
    "host.recovered": _host,
    "session.joined": _session_joined,
    "session.state_changed": _session_change,
    "session.recovered": _session_change,
    "session.quota_blocked": _session_change,
    "session.limit_warning": _session_change,
    "session.stuck": _session_change,
    "session.paused": _session_change,
    "session.resumed": _session_change,
    "session.left": _session_change,
    "session.lost": _session_change,
    "session.token_rotated": _noop,
    "activity.burst": _activity,
    "activity.commit": _activity,
    "activity.push": _activity,
    "activity.deploy": _activity,
    "activity.test_verdict_changed": _activity,
    "zone.created": _zone,
    "zone.updated": _zone,
    "zone.archived": _zone,
    "zone.frozen": _zone,
    "zone.unfrozen": _zone,
    "zone.synced": _noop,
    "zone.change_pending": _zone_change,
    "zone.change_decided": _zone_change,
    "zone.suggested_applied": _noop,
    "claim.requested": _claim,
    "claim.granted": _claim,
    "claim.queued": _claim,
    "claim.denied": _claim,
    "claim.released": _claim,
    "claim.expired": _claim,
    "claim.reserved": _claim,
    "claim.adopted": _claim,
    "claim.offered_in_brief": _claim_offered,
    "claim.handover_offered": _claim,
    "claim.handover_accepted": _claim,
    "claim.handover_declined": _claim,
    "claim.revoked": _claim,
    "claim.transferred": _claim,
    "claim.fenced": _claim_fenced,
    "claim.unconfirmed": _claim,
    "baton.passed": _baton_passed,
    "baton.ref_created": _baton_ref,
    "guard.blocked": _guard,
    "guard.bypass_used": _noop,
    "guard.tamper_blocked": _guard,
    "gate.error": _noop,
    "gate.deadline": _noop,
    "gate.tampered": _noop,
    "githook.missing": _githook,
    "collision.detected": _collision,
    "collision.acknowledged": _collision,
    "collision.resolved": _collision,
    "collision.dismissed": _collision,
    "collision.escalated": _collision,
    "task.created": _task,
    "task.updated": _task,
    "task.status_changed": _task,
    "task.assigned": _task,
    "task.stalled": _task,
    "task.recovered": _task,
    "task.review_requested": _task,
    "task.review_decided": _task,
    "task.done": _task,
    "task.reopened": _task,
    "task.deps_changed": _task,
    "task.acceptance_changed": _task,
    "checkpoint.created": _checkpoint,
    "checkpoint.missed": _noop,
    "report.submitted": _report,
    "report.accepted": _report,
    "report.rejected": _report,
    "report.waived": _report,
    "report.superseded": _report,
    "handoff.created": _noop,
    "message.posted": _message,
    "message.edited": _message,
    "message.redacted": _message,
    "decision.proposed": _decision,
    "decision.confirmed": _decision,
    "decision.rejected": _decision,
    "decision.superseded": _decision,
    "inbox.item_created": _inbox,
    "inbox.item_claimed": _inbox,
    "inbox.item_resolved": _inbox,
    "human.override": _noop,
    "budget.warning": _budget,
    "budget.cap_reached": _budget,
}
