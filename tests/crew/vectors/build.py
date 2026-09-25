"""Generate the crew contract vector files (WP-0a).

Run from the repo root to regenerate after changing an expectation:

    PYTHONPATH=src python -m tests.crew.vectors.build

``test_vectors_in_sync.py`` fails if the JSON files on disk differ from what
this script produces, so the files and their source never drift. Expected
values are literals written here by hand; nothing calls the reducer, the guard
evaluator or any other implementation under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from tests.crew.vectors import build_corpora
from tests.crew.vectors.fixtures import (
    CREW,
    OTHER_CREW,
    USER,
    base_snapshot,
    claim,
    collision,
    decision,
    event,
    frame,
    inbox_item,
    message,
    report,
    session,
    task,
    ts,
    zone,
)

VECTOR_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Assertion helpers (the reducer vector assertion language, see docs/crew/reducer.md)
# ---------------------------------------------------------------------------


def _path(dotted: str) -> list[str | int]:
    return [int(p) if p.isdigit() else p for p in dotted.split(".")]


def eq(path: str | list[str | int], value: Any) -> dict[str, Any]:
    return {"path": path if isinstance(path, list) else _path(path), "equals": value}


def absent(path: str | list[str | int]) -> dict[str, Any]:
    return {"path": path if isinstance(path, list) else _path(path), "absent": True}


def length(path: str, n: int) -> dict[str, Any]:
    return {"path": _path(path) if path else [], "length": n}


A, B, C = ("session", "cs_a"), ("session", "cs_b"), ("session", "cs_c")
HUMAN = ("human", USER)
REF = "refs/remembra/baton/T-14/7"


def vec(name: str, description: str, frames: list[dict[str, Any]], expect: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "description": description,
        "snapshot": kw.pop("snapshot", base_snapshot()),
        "frames": frames,
        "expect": expect,
    }
    out.update(kw)
    return out


def reducer_vectors() -> list[dict[str, Any]]:
    v: list[dict[str, Any]] = []
    E = event  # noqa: N806

    v.append(
        vec(
            "snapshot_init",
            "State from a snapshot: live claims/collisions/decisions only, presence empty, counts copied.",
            [],
            [
                eq("last_seq", 10),
                eq("mode", "multi"),
                eq("needs_resync", False),
                eq("resync_reason", None),
                eq("sessions.cs_a.presence", None),
                eq("sessions.cs_b.adapter_enforcement", "advisory"),
                eq("claims.clm_pos.state", "active"),
                absent("claims.clm_old"),
                eq("collisions.col_1.state", "open"),
                absent("collisions.col_2"),
                eq("decisions.dec_1.state", "in_force"),
                eq("decisions.dec_2.state", "proposed"),
                absent("decisions.dec_3"),
                eq("inbox_counts", {"project": 2, "crew": 1}),
                eq("pending_zone_changes.zch_1.loosening", None),
                length("messages", 0),
                length("moments", 0),
            ],
        )
    )

    reserved = claim(
        "clm_pos",
        "cs_a",
        state="reserved",
        zone_id="zn_pos",
        task_id="tsk_14",
        source="task",
        reserve_reason="quota",
        baton_ref=REF,
        lease_expires_at=None,
        version=2,
    )
    offer_frames = [
        E(
            11,
            "session.quota_blocked",
            {"error": "billing_error", "source": "reported", "baton_ref": REF, "claims_reserved": ["clm_pos"]},
            by=A,
        ),
        E(12, "baton.ref_created", {"ref": REF, "task_id": "tsk_14", "dirty_files": 3, "unpushed": 2}, by=A),
        E(13, "claim.reserved", {"claim": reserved, "reason": "quota"}, by=("system", "server")),
        E(
            14,
            "task.stalled",
            {
                "task": task(
                    "tsk_14",
                    14,
                    "stalled",
                    status_before_stall="in_progress",
                    zone_ids=["zn_pos"],
                    owner_session_id="cs_a",
                    version=2,
                ),
                "reason": "quota",
            },
        ),
        E(15, "session.joined", {"session": session("cs_c", "cc-2"), "resume_of": None, "observe_only": False}, by=C),
        E(
            16,
            "claim.offered_in_brief",
            {"offer": {"id": "off_1", "claim_id": "clm_pos", "task_id": "tsk_14", "to_session": "cs_c", "via": "brief"}},
            by=C,
        ),
    ]
    v.append(
        vec(
            "quota_reserve_and_offer",
            "Credits run out: session quota_blocked, baton ref, reserved claim, stalled task, offer recorded for cs_c.",
            [frame(e) for e in offer_frames],
            [
                eq("sessions.cs_a.state", "quota_blocked"),
                eq("sessions.cs_a.state_reason", "billing_error"),
                eq(["baton_refs", REF, "dirty_files"], 3),
                eq(["baton_refs", REF, "unpushed"], 2),
                eq(["baton_refs", REF, "session_id"], "cs_a"),
                eq("claims.clm_pos.state", "reserved"),
                eq("claims.clm_pos.reserve_reason", "quota"),
                eq("tasks.tsk_14.status", "stalled"),
                eq("tasks.tsk_14.status_before_stall", "in_progress"),
                eq("sessions.cs_c.presence", None),
                eq("offers.off_1.to_session", "cs_c"),
                length("moments", 1),
                eq("moments.0.type", "session.quota_blocked"),
                eq("last_seq", 16),
            ],
        )
    )

    adopted = claim("clm_pos", "cs_c", zone_id="zn_pos", task_id="tsk_14", source="adopt", epoch=2, version=3, baton_ref=REF)
    adopt_frames = [
        *offer_frames,
        E(17, "claim.adopted", {"claim": adopted, "cross_checkout": True, "from_session": "cs_a"}, by=C),
        E(
            18,
            "baton.passed",
            {
                "baton_id": "bat_1",
                "task_id": "tsk_14",
                "from_session": "cs_a",
                "to_session": "cs_c",
                "kind": "adopt",
                "handoff_id": "mem_42",
                "zones": ["zn_pos"],
                "baton_ref": REF,
                "restored": True,
            },
            by=C,
        ),
        E(19, "claim.released", {"claim": {**adopted, "state": "released", "version": 4}, "baton": False}, by=C),
    ]
    v.append(
        vec(
            "adopt_then_release",
            "Offered session adopts across checkouts (moment), baton passes (moment), then releases; offers cleared.",
            [frame(e) for e in adopt_frames],
            [
                absent("offers.off_1"),
                length("offers", 0),
                absent("claims.clm_pos"),
                length("batons", 1),
                eq("batons.0.seq", 18),
                eq("batons.0.to_session", "cs_c"),
                eq("batons.0.restored", True),
                eq("batons.0.kind", "adopt"),
                length("moments", 3),
                eq("moments.0.seq", 11),
                eq("moments.1.type", "claim.adopted"),
                eq("moments.2.type", "baton.passed"),
                eq("last_seq", 19),
                eq("crew.last_seq", 19),
            ],
        )
    )

    granted = claim("clm_rep", "cs_b", zone_id="zn_reports")
    v.append(
        vec(
            "duplicates_and_old_seq_ignored",
            "seq <= last_seq is ignored (replay overlap), even with different content.",
            [
                frame(E(11, "claim.granted", {"claim": granted}, by=B)),
                frame(E(11, "claim.queued", {"claim": {**granted, "state": "queued", "queue_pos": 1}}, by=B)),
                frame(E(9, "task.created", {"task": task("tsk_x", 99)})),
            ],
            [eq("claims.clm_rep.state", "active"), absent("tasks.tsk_x"), eq("last_seq", 11)],
        )
    )

    gap_frames = [
        frame(E(11, "task.updated", {"task": task("tsk_9", 9, "ready", title="Renamed", version=2), "changed": ["title"]})),
        frame(E(13, "claim.granted", {"claim": claim("clm_g", "cs_b", zone_id="zn_reports")}, by=B)),
        frame(E(12, "zone.created", {"zone": zone("zn_pay", "payroll", ["src/app/payroll/**"])})),
    ]
    v.append(
        vec(
            "gap_sets_resync",
            "seq > last_seq + 1 sets needs_resync; every later event is ignored until a snapshot frame.",
            gap_frames,
            [
                eq("needs_resync", True),
                eq("resync_reason", "gap"),
                eq("last_seq", 11),
                eq("tasks.tsk_9.title", "Renamed"),
                absent("claims.clm_g"),
                absent("zones.zn_pay"),
            ],
        )
    )
    snap2 = base_snapshot(
        as_of_seq=13,
        crew={**base_snapshot()["crew"], "last_seq": 13},
        claims=[claim("clm_g", "cs_b", zone_id="zn_reports")],
    )
    v.append(
        vec(
            "snapshot_frame_resets",
            "A snapshot frame replaces the whole state and clears needs_resync.",
            [
                *gap_frames,
                {"type": "snapshot", "data": snap2},
                frame(E(14, "session.stuck", {"signal": "no_progress", "stuck": True}, by=B)),
            ],
            [
                eq("needs_resync", False),
                eq("resync_reason", None),
                eq("last_seq", 14),
                eq("claims.clm_g.state", "active"),
                absent("claims.clm_pos"),
                eq("tasks.tsk_9.title", "Task 9"),
                eq("sessions.cs_b.stuck", True),
            ],
        )
    )
    v.append(
        vec(
            "server_resync_frame",
            "resync_required from the server behaves like a gap.",
            [
                {"type": "resync_required", "crew_id": CREW, "reason": "overflow", "last_seq": 40},
                frame(E(11, "task.created", {"task": task("tsk_y", 50)})),
            ],
            [eq("needs_resync", True), eq("resync_reason", "server"), eq("last_seq", 10), absent("tasks.tsk_y")],
        )
    )
    v.append(
        vec(
            "other_crew_ignored",
            "Events, presence and resync frames for another crew never touch this state.",
            [
                frame(E(11, "task.created", {"task": task("tsk_z", 51)}, crew_id=OTHER_CREW)),
                {"type": "resync_required", "crew_id": OTHER_CREW, "reason": "overflow", "last_seq": 5},
                {
                    "type": "presence",
                    "crew_id": OTHER_CREW,
                    "lanes": [
                        {
                            "session_id": "cs_a",
                            "state": "idle",
                            "stuck": True,
                            "last_action": None,
                            "calls_since_checkpoint": 1,
                            "next_checkpoint_due_at": None,
                            "limit": None,
                        }
                    ],
                },
            ],
            [eq("last_seq", 10), absent("tasks.tsk_z"), eq("needs_resync", False), eq("sessions.cs_a.presence", None)],
        )
    )
    lane_a = {
        "session_id": "cs_a",
        "state": "idle",
        "stuck": False,
        "last_action": {"tool": "Edit", "path_rel": "src/app/pos/Receipt.tsx", "verb": None, "age_s": 3},
        "calls_since_checkpoint": 17,
        "next_checkpoint_due_at": ts(900),
        "limit": {"level": "warn", "pct": 0.82, "source": "detected"},
    }
    v.append(
        vec(
            "presence_overlay",
            "Presence frames replace the ephemeral overlay of known sessions only and never change state or seq.",
            [
                {"type": "presence", "crew_id": CREW, "lanes": [lane_a, {**lane_a, "session_id": "cs_zzz"}]},
                {"type": "presence", "crew_id": CREW, "lanes": [{**lane_a, "calls_since_checkpoint": 18}]},
            ],
            [
                eq("sessions.cs_a.state", "active"),
                eq("sessions.cs_a.presence.state", "idle"),
                eq("sessions.cs_a.presence.calls_since_checkpoint", 18),
                eq("sessions.cs_a.presence.last_action.path_rel", "src/app/pos/Receipt.tsx"),
                eq("sessions.cs_a.presence.limit.pct", 0.82),
                absent("sessions.cs_a.presence.session_id"),
                absent("sessions.cs_zzz"),
                eq("last_seq", 10),
            ],
        )
    )
    v.append(
        vec(
            "session_lifecycle",
            "joined → quiet(host_unreachable) → recovered → limit → stuck → left; "
            "pause/resume by a human; lost; unknown ignored.",
            [
                frame(e)
                for e in (
                    E(11, "session.joined", {"session": session("cs_c", "cc-2"), "resume_of": None, "observe_only": False}, by=C),
                    E(
                        12,
                        "session.state_changed",
                        {"from": "active", "to": "quiet", "reason": "heartbeat_missing", "quiet_reason": "host_unreachable"},
                        refs={"session_id": "cs_c"},
                    ),
                    E(
                        13,
                        "session.recovered",
                        {"from": "quiet", "down_s": 240, "claims_retaken": [], "tasks_restored": [], "superseded_report_ids": []},
                        refs={"session_id": "cs_c"},
                    ),
                    E(
                        14,
                        "session.limit_warning",
                        {"level": "warn", "pct": 0.82, "source": "detected"},
                        refs={"session_id": "cs_c"},
                    ),
                    E(15, "session.stuck", {"signal": "no_progress", "stuck": True}, refs={"session_id": "cs_c"}),
                    E(16, "session.paused", {"reason": "checking"}, by=HUMAN, refs={"session_id": "cs_b"}),
                    E(17, "session.resumed", {"to": "idle", "reason": None}, by=HUMAN, refs={"session_id": "cs_b"}),
                    E(18, "session.left", {"reason": "clear", "claims_released": [], "claims_reserved": []}, by=C),
                    E(19, "session.lost", {"reason": "process_exited", "last_signal_age_s": 31}, refs={"session_id": "cs_a"}),
                    E(
                        20,
                        "session.state_changed",
                        {"from": "active", "to": "idle", "reason": "no_activity", "quiet_reason": None},
                        refs={"session_id": "cs_q"},
                    ),
                )
            ],
            [
                eq("sessions.cs_c.state", "ended"),
                eq("sessions.cs_c.end_reason", "clear"),
                eq("sessions.cs_c.ended_at", ts(18)),
                eq("sessions.cs_c.quiet_reason", None),
                eq("sessions.cs_c.limit", {"level": "warn", "pct": 0.82, "source": "detected"}),
                eq("sessions.cs_c.stuck", True),
                eq("sessions.cs_b.state", "idle"),
                eq("sessions.cs_a.state", "lost"),
                eq("sessions.cs_a.state_reason", "process_exited"),
                absent("sessions.cs_q"),
                length("moments", 4),
                eq("moments.0.type", "session.recovered"),
                eq("moments.1.type", "session.paused"),
                eq("moments.2.type", "session.resumed"),
                eq("moments.3.type", "session.lost"),
                eq("last_seq", 20),
            ],
        )
    )
    v.append(
        vec(
            "report_supersede_on_recovery",
            "Stalled report is current, recovery supersedes it, the completion becomes current; a stale supersede is a no-op.",
            [
                frame(e)
                for e in (
                    E(11, "report.submitted", {"report": report("rpt_s", "tsk_14", "stalled", facts_source="server-inferred")}),
                    E(
                        12,
                        "session.recovered",
                        {
                            "from": "lost",
                            "down_s": 300,
                            "claims_retaken": ["clm_pos"],
                            "tasks_restored": ["tsk_14"],
                            "superseded_report_ids": ["rpt_s"],
                        },
                        refs={"session_id": "cs_a"},
                    ),
                    E(
                        13,
                        "report.superseded",
                        {
                            "report": report(
                                "rpt_s",
                                "tsk_14",
                                "stalled",
                                is_current=False,
                                superseded_reason="recovered",
                                facts_source="server-inferred",
                            )
                        },
                    ),
                    E(14, "task.recovered", {"task": task("tsk_14", 14, "in_progress", owner_session_id="cs_a", version=3)}),
                    E(15, "report.submitted", {"report": report("rpt_c", "tsk_14", "completion", review_state="accepted")}, by=A),
                    E(
                        16,
                        "task.done",
                        {"task": task("tsk_14", 14, "done", current_report_id="rpt_c", version=4), "report_id": "rpt_c"},
                    ),
                    E(17, "report.superseded", {"report": report("rpt_old", "tsk_14", "partial", is_current=False)}),
                )
            ],
            [
                eq("reports.tsk_14.id", "rpt_c"),
                eq("reports.tsk_14.verdict", "complete"),
                eq("tasks.tsk_14.status", "done"),
                eq("tasks.tsk_14.current_report_id", "rpt_c"),
                length("moments", 2),
                eq("moments.0.type", "session.recovered"),
                eq("moments.1.type", "task.done"),
            ],
        )
    )
    v.append(
        vec(
            "inbox_counts",
            "Counts adjust for items created after the snapshot and for pre-snapshot items resolved later; "
            "session items are not counted.",
            [
                frame(e)
                for e in (
                    E(11, "inbox.item_created", {"item": inbox_item("inb_new", "project")}),
                    E(12, "inbox.item_resolved", {"item": inbox_item("inb_pre1", "project", state="resolved")}, by=HUMAN),
                    E(
                        13,
                        "inbox.item_claimed",
                        {"item": inbox_item("inb_pre2", "crew", state="claimed", kind="task_ready", claimed_by="cs_b")},
                        by=B,
                    ),
                    E(
                        14,
                        "inbox.item_resolved",
                        {"item": inbox_item("inb_pre2", "crew", state="resolved", kind="task_ready")},
                        by=B,
                    ),
                    E(
                        15,
                        "inbox.item_resolved",
                        {"item": inbox_item("inb_x", "crew", state="resolved", kind="task_ready")},
                        by=B,
                    ),
                    E(16, "inbox.item_created", {"item": inbox_item("inb_s", "session", kind="mention", recipient="cs_a")}),
                    E(17, "inbox.item_resolved", {"item": inbox_item("inb_new", "project", state="dismissed")}, by=HUMAN),
                )
            ],
            [
                eq("inbox_counts", {"project": 1, "crew": 0}),
                eq("inbox.inb_s.state", "open"),
                absent("inbox.inb_new"),
                absent("inbox.inb_pre2"),
                length("inbox", 1),
            ],
        )
    )
    msg_frames = [
        frame(E(10 + i, "message.posted", {"message": message(f"msg_{i:04d}", 10 + i, f"m{i}")}, by=A)) for i in range(1, 103)
    ]
    msg_frames.append(
        frame(
            E(113, "message.edited", {"message": message("msg_0100", 110, "edited", edited=True), "after_delivery": True}, by=A)
        )
    )
    msg_frames.append(frame(E(114, "message.redacted", {"message_id": "msg_0101"}, by=HUMAN)))
    msg_frames.append(
        frame(E(115, "message.edited", {"message": message("msg_0001", 11, "gone", edited=True), "after_delivery": False}, by=A))
    )
    v.append(
        vec(
            "messages_cap_edit_redact",
            "The last 100 messages by seq are kept; edits replace, redaction blanks the body; "
            "edits of evicted messages are ignored.",
            msg_frames,
            [
                length("messages", 100),
                eq("messages.0.id", "msg_0003"),
                eq("messages.99.id", "msg_0102"),
                eq("messages.97.id", "msg_0100"),
                eq("messages.97.body", "edited"),
                eq("messages.97.edited", True),
                eq("messages.98.body", ""),
                eq("messages.98.redacted", True),
                eq("last_seq", 115),
                length("moments", 1),
                eq("moments.0.type", "message.redacted"),
            ],
        )
    )
    v.append(
        vec(
            "decisions_flow",
            "Agent decisions stay proposed until a human confirms; rejected and superseded leave the live set.",
            [
                frame(e)
                for e in (
                    E(11, "decision.proposed", {"decision": decision("dec_9", 9, "proposed")}, by=A),
                    E(
                        12,
                        "decision.confirmed",
                        {"decision": decision("dec_9", 9, "in_force", decided_by_kind="agent", decided_by="cs_a")},
                        by=HUMAN,
                    ),
                    E(13, "decision.superseded", {"decision": decision("dec_1", 1, "superseded")}, by=HUMAN),
                    E(14, "decision.rejected", {"decision": decision("dec_2", 2, "rejected")}, by=HUMAN),
                )
            ],
            [
                eq("decisions.dec_9.state", "in_force"),
                eq("decisions.dec_9.confirmed_by", USER),
                absent("decisions.dec_1"),
                absent("decisions.dec_2"),
                length("decisions", 1),
                length("moments", 3),
            ],
        )
    )
    v.append(
        vec(
            "collisions_flow",
            "High severity detections are moments; acknowledged stays live; resolved/dismissed leave; escalation flags.",
            [
                frame(e)
                for e in (
                    E(11, "collision.detected", {"collision": collision("col_9", "exclusive_breach", attribution="probable")}),
                    E(
                        12,
                        "collision.acknowledged",
                        {"collision": collision("col_9", "exclusive_breach", state="acknowledged")},
                        by=A,
                    ),
                    E(13, "collision.escalated", {"collision": collision("col_1", "same_file", escalated=True)}),
                    E(14, "collision.detected", {"collision": collision("col_8", "same_zone_shared")}),
                    E(15, "collision.resolved", {"collision": collision("col_9", "exclusive_breach", state="resolved")}),
                    E(
                        16,
                        "collision.dismissed",
                        {"collision": collision("col_8", "same_zone_shared", state="dismissed")},
                        by=HUMAN,
                    ),
                )
            ],
            [
                absent("collisions.col_9"),
                absent("collisions.col_8"),
                eq("collisions.col_1.escalated", True),
                length("collisions", 1),
                length("moments", 2),
                eq("moments.0.seq", 11),
                eq("moments.1.seq", 16),
            ],
        )
    )
    v.append(
        vec(
            "zones_and_policy",
            "Zone upserts, freeze by a human, archive, pending loosening change, decided change removed.",
            [
                frame(e)
                for e in (
                    E(11, "zone.created", {"zone": zone("zn_pay", "payroll", ["src/app/payroll/**"])}),
                    E(
                        12,
                        "zone.frozen",
                        {
                            "zone": zone(
                                "zn_pos", "pos", ["src/app/pos/**"], frozen_by=USER, frozen_note="Mani editing", version=2
                            ),
                            "reason": "hands on",
                        },
                        by=HUMAN,
                    ),
                    E(13, "zone.archived", {"zone": zone("zn_reports", "reports", ["src/app/reports/**"])}),
                    E(
                        14,
                        "zone.change_pending",
                        {"change_id": "zch_2", "sha": "abc123", "loosening": True, "diff_summary": "remove zone pos"},
                    ),
                    E(15, "zone.change_decided", {"change_id": "zch_1", "decision": "approved"}, by=HUMAN),
                    E(
                        16,
                        "zone.synced",
                        {
                            "sha": "def456",
                            "branch": "main",
                            "diff_summary": "add payroll",
                            "policy_changed": True,
                            "zone_ids": ["zn_pay"],
                        },
                    ),
                )
            ],
            [
                eq("zones.zn_pay.slug", "payroll"),
                eq("zones.zn_pos.frozen_by", USER),
                absent("zones.zn_reports"),
                eq("pending_zone_changes.zch_2.loosening", True),
                absent("pending_zone_changes.zch_1"),
                length("moments", 4),
            ],
        )
    )
    v.append(
        vec(
            "guard_hooks_fence_activity",
            "Client guard events count per session (coalesced), tamper blocks are moments, githook state, fencing, commits.",
            [
                frame(e)
                for e in (
                    E(
                        11,
                        "guard.blocked",
                        {
                            "path_rel": "src/app/pos/cart.ts",
                            "zone": "pos",
                            "holder": "cc-1",
                            "rule": 9,
                            "op": "write",
                            "decision": "deny",
                            "surface": "pretool",
                            "coalesced": 3,
                        },
                        by=B,
                        origin="client",
                    ),
                    E(
                        12,
                        "guard.blocked",
                        {
                            "path_rel": "src/app/pos/split.ts",
                            "zone": "pos",
                            "holder": "cc-1",
                            "rule": 9,
                            "op": "write",
                            "decision": "deny",
                            "surface": "precommit",
                            "coalesced": 1,
                        },
                        by=B,
                        origin="client",
                    ),
                    E(13, "guard.tamper_blocked", {"kind": "no_verify", "surface": "pretool"}, by=B, origin="client"),
                    E(
                        14,
                        "githook.missing",
                        {"hook": "pre-push", "state": "missing", "worktree_id": "wt-cc-1"},
                        by=A,
                        origin="client",
                    ),
                    E(15, "claim.fenced", {"claim_id": "clm_pos", "horizon_at": ts(540)}),
                    E(
                        16,
                        "activity.commit",
                        {
                            "sha": "a1b2c3d4e5f6",
                            "subject_hash": "0123456789abcdef",
                            "files": ["src/app/pos/cart.ts"],
                            "branch": "feat/cc-1",
                        },
                        by=A,
                        origin="client",
                    ),
                    E(
                        17,
                        "activity.burst",
                        {"files_touched": ["x.ts"], "command_verbs": ["npm"], "tests": {"pass": 1, "fail": 0}},
                        by=("session", "cs_q"),
                        origin="client",
                    ),
                )
            ],
            [
                eq("guard_blocks.cs_b", 4),
                eq("tamper_blocks.cs_b", 1),
                eq("sessions.cs_a.githook_state", "missing"),
                eq("claims.clm_pos.fenced", True),
                eq("sessions.cs_a.head_commit", "a1b2c3d4e5f6"),
                eq("sessions.cs_a.last_activity_at", ts(16)),
                absent("sessions.cs_q"),
                length("moments", 1),
                eq("moments.0.type", "guard.tamper_blocked"),
                eq("last_seq", 17),
            ],
        )
    )
    v.append(
        vec(
            "moments_cap",
            "Only the last 50 moments are kept, in seq order.",
            [
                frame(E(10 + i, "guard.tamper_blocked", {"kind": "husky_off", "surface": "pretool"}, by=B, origin="client"))
                for i in range(1, 61)
            ],
            [length("moments", 50), eq("moments.0.seq", 21), eq("moments.49.seq", 70), eq("tamper_blocks.cs_b", 60)],
        )
    )
    unknown = E(11, "task.created", {"task": task("tsk_u", 60)})
    unknown["type"] = "crew.future_event"
    newer = E(12, "claim.granted", {"claim": claim("clm_v2", "cs_b", zone_id="zn_reports")}, by=B, v=2)
    v.append(
        vec(
            "unknown_type_and_version_advance_seq",
            "An unknown type or an unknown version only advances last_seq (a newer server must not stall old clients).",
            [frame(unknown), frame(newer), frame(E(13, "task.created", {"task": task("tsk_after", 61)}))],
            [eq("last_seq", 13), absent("claims.clm_v2"), eq("tasks.tsk_after.number", 61), absent("tasks.tsk_u")],
            events_validate=False,
        )
    )
    v.append(
        vec(
            "crew_hosts_budget_checkpoints",
            "Crew mode/settings, host reachability, budget meters, latest checkpoint per session.",
            [
                frame(e)
                for e in (
                    E(11, "crew.mode_changed", {"from": "multi", "to": "solo", "live_sessions": 1}),
                    E(
                        12,
                        "crew.settings_changed",
                        {"settings_version": 2, "changed_keys": ["enforcement"], "enforcement": "observe"},
                        by=HUMAN,
                    ),
                    E(
                        13,
                        "host.registered",
                        {
                            "host": {
                                "id": "hst_mbp",
                                "host_label": "mbp",
                                "platform": "darwin",
                                "crewd_version": "1.0.0",
                                "state": "online",
                            }
                        },
                    ),
                    E(14, "host.unreachable", {"host_id": "hst_mbp", "silent_s": 200, "session_ids": ["cs_a"]}),
                    E(15, "host.recovered", {"host_id": "hst_mbp", "down_s": 300}),
                    E(16, "budget.warning", {"metric": "events_per_day", "used": 4000, "limit": 5000}),
                    E(17, "budget.cap_reached", {"metric": "memory_promotions", "used": 50, "limit": 50}),
                    E(
                        18,
                        "checkpoint.created",
                        {
                            "checkpoint": {
                                "id": "ckp_1",
                                "session_id": "cs_a",
                                "task_id": "tsk_14",
                                "trigger": "commit",
                                "headline": "c1",
                                "facts_source": "relay-cli",
                            }
                        },
                        by=A,
                    ),
                    E(
                        19,
                        "checkpoint.created",
                        {
                            "checkpoint": {
                                "id": "ckp_2",
                                "session_id": "cs_a",
                                "task_id": "tsk_14",
                                "trigger": "turn",
                                "headline": "c2",
                                "facts_source": "relay-cli",
                            }
                        },
                        by=A,
                    ),
                    E(20, "crew.mode_changed", {"from": "solo", "to": "multi", "live_sessions": 2}),
                )
            ],
            [
                eq("mode", "multi"),
                eq("crew.mode", "multi"),
                eq("crew.settings_version", 2),
                eq("crew.enforcement", "observe"),
                eq("hosts.hst_mbp.state", "online"),
                eq("budget.events_per_day", {"used": 4000, "limit": 5000, "capped": False}),
                eq("budget.memory_promotions.capped", True),
                eq("checkpoints.cs_a.id", "ckp_2"),
                length("moments", 3),
                eq("moments.0.type", "crew.settings_changed"),
                eq("moments.1.type", "host.unreachable"),
                eq("moments.2.type", "crew.mode_changed"),
            ],
        )
    )
    return v


# ---------------------------------------------------------------------------
# Guard table vectors (facts → first-match outcome)
# ---------------------------------------------------------------------------


def g(
    name: str,
    facts: dict[str, Any],
    mode: str,
    rule: int,
    decision: str,
    variant: str = "",
    effects: list[str] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    case = {
        "name": name,
        "mode": mode,
        "facts": facts,
        "expect": {"rule": rule, "decision": decision, "variant": variant, "effects": effects or []},
    }
    case.update(kw)
    return case


def guard_table_vectors() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []
    GB, WD = ["guard.blocked"], ["would_deny"]  # noqa: N806
    single = [
        # (row, predicate, enforce decision, enforce variant, observe decision, observe variant)
        (1, "session_paused", "deny", "paused", "deny", "paused"),
        (2, "crew_policy_target", "deny", "crew_policy", "deny", "crew_policy"),
        (3, "zone_protected_no_grant", "deny", "protected", "deny", "protected"),
        (4, "foreign_checkout", "deny", "", "deny", ""),
        (5, "same_worktree_dirty_elsewhere", "deny", "", "deny", ""),
        (6, "path_ignored", "allow", "", "allow", ""),
        (7, "own_claim_lease_ok", "allow", "", "allow", ""),
        (8, "own_claim_lease_passed", "deny", "lease_unconfirmed", "warn", "lease_unconfirmed"),
        (9, "exclusive_held_by_other", "deny", "", "warn", ""),
        (11, "append_only_edit_existing", "deny", "", "warn", ""),
        (13, "tree_writer_other_exclusive", "deny", "", "warn", ""),
        (14, "service_claimed_by_other", "deny", "", "warn", ""),
        (15, "tree_git_op_other_live_same_checkout", "deny", "", "warn", ""),
        (18, "parent_zone_unclaimed", "deny", "task_required", "warn", "task_required"),
    ]
    for row, pred, ed, ev, od, ov in single:
        eff_e = ["guard.tamper_blocked"] if row == 2 else (GB if ed == "deny" else [])
        eff_o = ["guard.tamper_blocked"] if row == 2 else (GB if od == "deny" else (WD if od == "warn" else []))
        c.append(g(f"row{row}_{pred}_enforce", {pred: True}, "enforce", row, ed, ev, eff_e))
        c.append(g(f"row{row}_{pred}_observe", {pred: True}, "observe", row, od, ov, eff_o))
    c += [
        g("row2_tamper_command", {"tamper_command": True}, "enforce", 2, "deny", "tamper", ["guard.tamper_blocked"]),
        g("row3_frozen", {"zone_frozen": True}, "enforce", 3, "deny", "frozen", GB),
        g("row3_frozen_wins_variant", {"zone_frozen": True, "zone_protected_no_grant": True}, "observe", 3, "deny", "frozen", GB),
        g("row10_reserved_not_offered", {"reserved_for_other": True}, "enforce", 10, "deny", "reserved_not_offered", GB),
        g(
            "row10_reserved_offered",
            {"reserved_for_other": True, "holds_offer": True},
            "enforce",
            10,
            "deny",
            "reserved_offered",
            GB,
        ),
        g("row10_observe", {"reserved_for_other": True}, "observe", 10, "warn", "reserved_not_offered", WD),
        g("row12_plain", {"commons": True}, "enforce", 12, "allow", "", ["notify_watchers"]),
        g(
            "row12_plain_explicit",
            {"commons": True, "commons_kind": "plain", "creates_file": True},
            "observe",
            12,
            "allow",
            "",
            ["notify_watchers"],
        ),
        g(
            "row12_serialize",
            {"commons": True, "commons_kind": "serialize"},
            "enforce",
            12,
            "allow",
            "",
            ["notify_watchers", "micro_lease"],
        ),
        g(
            "row12_append_only_new",
            {"commons": True, "commons_kind": "append_only", "creates_file": True},
            "enforce",
            12,
            "allow",
            "",
            ["notify_watchers", "micro_lease"],
        ),
        g(
            "row12_append_only_migration",
            {"commons": True, "commons_kind": "append_only", "creates_file": True, "migration": True},
            "enforce",
            12,
            "allow",
            "",
            ["notify_watchers", "micro_lease", "schema_claim"],
        ),
        g("row16_shared_claim", {"shared_claim_by_others": True}, "enforce", 16, "allow", "", ["collision:same_zone_shared"]),
        g("row16_dirty_elsewhere", {"dirty_in_other_checkout": True}, "observe", 16, "allow", "", ["collision:same_file"]),
        g(
            "row17_granted",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "granted"},
            "enforce",
            17,
            "allow",
            "auto_claimed",
            ["claim.granted"],
        ),
        g(
            "row17_granted_observe",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "granted"},
            "observe",
            17,
            "allow",
            "auto_claimed",
            ["claim.granted"],
        ),
        g(
            "row17_conflict",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "conflict"},
            "enforce",
            17,
            "deny",
            "claim_conflict",
            GB,
        ),
        g(
            "row17_conflict_observe",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "conflict"},
            "observe",
            17,
            "warn",
            "claim_conflict",
            WD,
        ),
        g("row17_cap", {"leaf_zone_unclaimed": True, "auto_claim_result": "cap"}, "enforce", 17, "deny", "claim_cap", GB),
        g(
            "row17_timeout_first_write",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "timeout"},
            "enforce",
            17,
            "allow",
            "allowed_unconfirmed",
            ["claim.unconfirmed", "gate.deadline"],
        ),
        g(
            "row17_rate_limited_first_write",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "rate_limited"},
            "enforce",
            17,
            "allow",
            "allowed_unconfirmed",
            ["claim.unconfirmed"],
        ),
        g(
            "row17_timeout_second_write_denied",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "timeout", "unconfirmed_pending": True},
            "enforce",
            17,
            "deny",
            "unconfirmed_pending",
            GB,
        ),
        g(
            "row17_rate_limited_second_write_denied",
            {"leaf_zone_unclaimed": True, "auto_claim_result": "rate_limited", "unconfirmed_pending": True},
            "enforce",
            17,
            "deny",
            "unconfirmed_pending",
            GB,
        ),
        g(
            "row17_disabled",
            {"leaf_zone_unclaimed": True, "auto_claim_enabled": False},
            "enforce",
            17,
            "deny",
            "claim_required",
            GB,
        ),
        g(
            "row17_disabled_observe",
            {"leaf_zone_unclaimed": True, "auto_claim_enabled": False},
            "observe",
            17,
            "warn",
            "claim_required",
            WD,
        ),
        g("row19_no_zone", {"no_zone_match": True}, "enforce", 19, "allow", "footprint", ["footprint"]),
        g("row19_empty_facts", {}, "enforce", 19, "allow", "footprint", ["footprint"]),
        g(
            "row19_file_claim_granted",
            {"no_zone_match": True, "undeclared_policy": "file_claim", "auto_claim_result": "granted"},
            "enforce",
            19,
            "allow",
            "file_auto_claimed",
            ["claim.granted"],
        ),
        g(
            "row19_file_claim_conflict",
            {"no_zone_match": True, "undeclared_policy": "file_claim", "auto_claim_result": "conflict"},
            "enforce",
            19,
            "deny",
            "file_claim_conflict",
            GB,
        ),
        # first-match order
        g(
            "order_paused_over_own_claim",
            {"session_paused": True, "own_claim_lease_ok": True},
            "enforce",
            1,
            "deny",
            "paused",
            GB,
        ),
        g(
            "order_policy_over_ignored",
            {"crew_policy_target": True, "path_ignored": True},
            "enforce",
            2,
            "deny",
            "crew_policy",
            ["guard.tamper_blocked"],
        ),
        g(
            "order_frozen_over_clobber",
            {"zone_frozen": True, "same_worktree_dirty_elsewhere": True},
            "enforce",
            3,
            "deny",
            "frozen",
            GB,
        ),
        g(
            "order_foreign_over_exclusive",
            {"foreign_checkout": True, "exclusive_held_by_other": True},
            "observe",
            4,
            "deny",
            "",
            GB,
        ),
        g(
            "order_clobber_over_ignored",
            {"same_worktree_dirty_elsewhere": True, "path_ignored": True},
            "enforce",
            5,
            "deny",
            "",
            GB,
        ),
        g("order_ignored_over_exclusive", {"path_ignored": True, "exclusive_held_by_other": True}, "enforce", 6, "allow", "", []),
        g(
            "order_own_claim_over_exclusive",
            {"own_claim_lease_ok": True, "exclusive_held_by_other": True},
            "enforce",
            7,
            "allow",
            "",
            [],
        ),
        g(
            "order_fenced_over_exclusive",
            {"own_claim_lease_passed": True, "exclusive_held_by_other": True},
            "enforce",
            8,
            "deny",
            "lease_unconfirmed",
            GB,
        ),
        g(
            "order_exclusive_over_reserved",
            {"exclusive_held_by_other": True, "reserved_for_other": True},
            "enforce",
            9,
            "deny",
            "",
            GB,
        ),
        g("order_append_only_over_commons", {"append_only_edit_existing": True, "commons": True}, "enforce", 11, "deny", "", GB),
        g(
            "order_commons_over_tree_writer",
            {"commons": True, "tree_writer_other_exclusive": True},
            "enforce",
            12,
            "allow",
            "",
            ["notify_watchers"],
        ),
        g(
            "order_tree_writer_over_service",
            {"tree_writer_other_exclusive": True, "service_claimed_by_other": True},
            "enforce",
            13,
            "deny",
            "",
            GB,
        ),
        g(
            "order_shared_over_leaf",
            {"shared_claim_by_others": True, "leaf_zone_unclaimed": True},
            "enforce",
            16,
            "allow",
            "",
            ["collision:same_zone_shared"],
        ),
        g(
            "order_leaf_over_parent",
            {"leaf_zone_unclaimed": True, "parent_zone_unclaimed": True, "auto_claim_result": "granted"},
            "enforce",
            17,
            "allow",
            "auto_claimed",
            ["claim.granted"],
        ),
        g(
            "order_parent_over_no_zone",
            {"parent_zone_unclaimed": True, "no_zone_match": True},
            "enforce",
            18,
            "deny",
            "task_required",
            GB,
        ),
    ]
    # interactive override
    for row, pred in (
        (9, "exclusive_held_by_other"),
        (13, "tree_writer_other_exclusive"),
        (14, "service_claimed_by_other"),
        (15, "tree_git_op_other_live_same_checkout"),
    ):
        c.append(
            g(f"ask_row{row}", {pred: True}, "enforce", row, "ask", "", [], interactive_override=True, permission_mode="default")
        )
    c += [
        g(
            "ask_row10_offered",
            {"reserved_for_other": True, "holds_offer": True},
            "enforce",
            10,
            "ask",
            "reserved_offered",
            [],
            interactive_override=True,
        ),
        g(
            "ask_not_in_bypass_permissions",
            {"exclusive_held_by_other": True},
            "enforce",
            9,
            "deny",
            "",
            GB,
            interactive_override=True,
            permission_mode="bypassPermissions",
        ),
        g(
            "ask_never_rows_1_to_5",
            {"zone_protected_no_grant": True},
            "enforce",
            3,
            "deny",
            "protected",
            GB,
            interactive_override=True,
        ),
        g("ask_never_foreign_checkout", {"foreign_checkout": True}, "enforce", 4, "deny", "", GB, interactive_override=True),
        g(
            "ask_not_row8",
            {"own_claim_lease_passed": True},
            "enforce",
            8,
            "deny",
            "lease_unconfirmed",
            GB,
            interactive_override=True,
        ),
        g(
            "ask_not_row18",
            {"parent_zone_unclaimed": True},
            "enforce",
            18,
            "deny",
            "task_required",
            GB,
            interactive_override=True,
        ),
        g("ask_observe_stays_warn", {"exclusive_held_by_other": True}, "observe", 9, "warn", "", WD, interactive_override=True),
        g(
            "ask_off_by_default",
            {"exclusive_held_by_other": True},
            "enforce",
            9,
            "deny",
            "",
            GB,
            interactive_override=False,
            permission_mode="default",
        ),
    ]
    return c


def events_samples() -> dict[str, Any]:
    """One valid envelope per L0 event type, plus invalid envelopes/client events with the reason they fail."""
    # The reducer vectors already exercise most types; build_corpora adds a sample for every remaining type.
    return build_corpora.event_samples()


def write_all(check: bool = False) -> list[str]:
    """Write (or, with ``check``, compare) every vector file. Returns the paths that differ."""
    files: dict[Path, Any] = {}
    for vector in reducer_vectors():
        files[VECTOR_DIR / "reducer" / f"{vector['name']}.json"] = vector
    files[VECTOR_DIR / "guard" / "table.json"] = {
        "description": "Facts → §5.2 first-match outcome. rule 0 never appears here (fast exit happens before the table).",
        "cases": guard_table_vectors(),
    }
    files[VECTOR_DIR / "guard" / "concrete.json"] = build_corpora.guard_concrete()
    files[VECTOR_DIR / "bash" / "corpus.json"] = build_corpora.bash_corpus()
    files[VECTOR_DIR / "mcp" / "tool_map.json"] = build_corpora.mcp_tool_map()
    files[VECTOR_DIR / "grammar" / "command_patterns.json"] = build_corpora.command_patterns()
    files[VECTOR_DIR / "hooks" / "stdout.json"] = build_corpora.hook_stdout()
    files[VECTOR_DIR / "hooks" / "agent_text.json"] = build_corpora.agent_text()
    files[VECTOR_DIR / "redaction" / "corpus.json"] = build_corpora.redaction_corpus()
    files[VECTOR_DIR / "events" / "samples.json"] = events_samples()
    changed: list[str] = []
    for path, data in sorted(files.items()):
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            changed.append(str(path.relative_to(VECTOR_DIR)))
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    if not check:
        expected = {p.resolve() for p in files}
        for stale in VECTOR_DIR.glob("*/*.json"):
            if stale.resolve() not in expected:
                changed.append(f"removed {stale.relative_to(VECTOR_DIR)}")
                stale.unlink()
    return changed


if __name__ == "__main__":
    diff = write_all(check="--check" in sys.argv)
    for line in diff:
        print(line)
    sys.exit(1 if diff and "--check" in sys.argv else 0)
