"""Wave-2 review fixes in sessions, close-out, tasks, the reaper and collisions (real crew.db, real event log).

Covers: S0 StopFailure/SessionEnd order (stall and leave commute), D14 classification (usage limit vs
transient 429 vs auth), the SessionEnd baton ref after a relay close, relay close needs the session
token, stall/leave facts through ``crew.redact.outbound``, heartbeat collision detection, queue
promotion on every ending path, blocked tasks on stall, micro-leases, and released batons.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from remembra.crew import claims as C
from remembra.crew import collisions  # noqa: F401  (registers the heartbeat footprint sink)
from remembra.crew.closeout import on_relay_close
from remembra.crew.reaper import CrewReaper
from remembra.crew.sessions import FOOTPRINT_SINKS, classify_stop_failure, get_session
from remembra.crew.store import crew_id_for
from remembra.crew.tasks import Caller, TaskService
from remembra.crew.zones import CrewOps, Principal
from tests.crew.sessions_support import (
    OWNER,
    PROJECT,
    add_claim,
    add_task,
    add_zone,
    hb_item,
    host,
    join_req,
    make_env,
)
from tests.crew.vectors.loader import load

CREW = crew_id_for(OWNER, PROJECT)
CAPTURES = Path(__file__).parent / "fixtures" / "captures" / "claude-code-2.1.168"
STOPFAILURE_RUNS = sorted(
    str(p.relative_to(CAPTURES)) for p in CAPTURES.glob("*/*stopfailure*") if any(p.glob("*-StopFailure.json"))
)


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path)
    yield e
    await e.db.close()


async def _join(env, sid, **kw):
    return await env.svc.join(user_id=OWNER, req=join_req(sid, **kw), host_token=None, session_token=None)


async def _holder(env, sid="s-a", *, status="in_progress", **kw):
    j = await _join(env, sid, **kw)
    zone = await add_zone(env, CREW, "pos")
    task = await add_task(env, CREW, 1, owner=j.session, zones=[zone], status=status)
    claim = await add_claim(env, CREW, j.session, zone_id=zone, task_id=task)
    return j, zone, task, claim


def _captured(run: str) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted((CAPTURES / run).glob("0*.json"))]


async def _replay(env, session, events):
    """SessionEnd → ``leave`` (crewd step 4), StopFailure → ``stall``, in the recorded order (§8.2)."""
    for ev in events:
        p = ev["payload"]
        if ev["event"] == "SessionEnd":
            await env.svc.leave(session, reason=p["reason"], facts={}, summary=None, baton=False, baton_ref=None)
        elif ev["event"] == "StopFailure":
            await env.svc.stall(
                session, error=p["error"], facts={}, baton_ref=None, last_assistant_message=p.get("last_assistant_message")
            )


async def _outcome(env, task, claim):
    types = [e["type"] for e in await env.events(CREW)]
    report = await env.one("SELECT kind FROM crew_reports WHERE task_id = ? AND is_current = 1", (task,))
    c = await env.one("SELECT state, reserve_reason FROM crew_claims WHERE id = ?", (claim,))
    outbox = [r["kind"] for r in await env.all("SELECT kind FROM crew_outbox")]
    return {
        "quota_blocked": "session.quota_blocked" in types,
        "report": report["kind"] if report else None,
        "claim": (c["state"], c["reserve_reason"]),
        "handoff": "relay_handoff" in outbox,
    }


EXPECTED_S0 = {
    # billing_error: out of credits → quota; the S0 capture had SessionEnd first
    "mock/stopfailure_billing": {"quota_blocked": True, "report": "stalled", "claim": ("reserved", "quota"), "handoff": True},
    "mock/stopfailure_slow_hook": {
        "quota_blocked": True,
        "report": "stalled",
        "claim": ("reserved", "quota"),
        "handoff": True,
    },
    # auth failures block (reason auth), in either order
    "mock/stopfailure_auth": {"quota_blocked": True, "report": "stalled", "claim": ("reserved", "quota"), "handoff": True},
    "real/real_stopfailure_model": {
        "quota_blocked": True,
        "report": "stalled",
        "claim": ("reserved", "quota"),
        "handoff": True,
    },
    # transient: checkpoint only, the SessionEnd decides (dirty/unfinished → ended_dirty partial)
    "mock/stopfailure_ratelimit": {
        "quota_blocked": False,
        "report": "partial",
        "claim": ("reserved", "ended_dirty"),
        "handoff": False,
    },
    "mock/stopfailure_overloaded": {
        "quota_blocked": False,
        "report": "partial",
        "claim": ("reserved", "ended_dirty"),
        "handoff": False,
    },
    "mock/stopfailure_model": {
        "quota_blocked": False,
        "report": "partial",
        "claim": ("reserved", "ended_dirty"),
        "handoff": False,
    },
    "mock/stopfailure_network": {
        "quota_blocked": False,
        "report": "partial",
        "claim": ("reserved", "ended_dirty"),
        "handoff": False,
    },
}


def test_every_s0_stopfailure_capture_is_covered():
    assert set(STOPFAILURE_RUNS) == set(EXPECTED_S0)


@pytest.mark.parametrize("run", STOPFAILURE_RUNS)
@pytest.mark.parametrize("order", ["recorded", "reversed"])
async def test_s0_stopfailure_and_sessionend_commute(tmp_path, run, order):
    """Every S0 StopFailure capture, replayed in its recorded order and reversed, reaches the same outcome."""
    env = await make_env(tmp_path)
    try:
        a, _zone, task, claim = await _holder(env)
        events = [e for e in _captured(run) if e["event"] in ("SessionEnd", "StopFailure")]
        if order == "reversed":
            events = events[::-1]
        await _replay(env, a.session, events)
        assert await _outcome(env, task, claim) == EXPECTED_S0[run]
        row = await get_session(env.conn, a.session["id"])
        assert row["state"] == "ended"
        await env.chain_ok(CREW)
    finally:
        await env.db.close()


async def test_a_late_quota_stall_raises_the_alert_items_and_the_stalled_handoff(env):
    a, _zone, task, claim = await _holder(env)
    await env.svc.leave(a.session, reason="other", facts={}, summary=None, baton=False, baton_ref=None)
    env.clock.advance(30)
    res = await env.svc.stall(
        a.session, error="billing_error", facts={}, baton_ref="refs/remembra/baton/T-1/7", last_assistant_message=None
    )
    assert res["late"] is True and res["claims_reserved"] == [claim] and res["tasks_stalled"] == [task]
    blocked = await env.events(CREW, types=("session.quota_blocked",))
    assert len(blocked) == 1 and blocked[0]["summary"] == "cc-1 out of credits (billing_error, reported)"
    assert blocked[0]["payload"]["claims_reserved"] == [claim] and blocked[0]["moment"]
    c = await env.one("SELECT reserve_reason, baton_ref FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"reserve_reason": "quota", "baton_ref": "refs/remembra/baton/T-1/7"}
    reports = await env.all("SELECT kind, is_current, baton_ref FROM crew_reports WHERE task_id = ? ORDER BY created_at", (task,))
    assert [(r["kind"], r["is_current"]) for r in reports] == [("partial", 0), ("stalled", 1)]
    assert reports[-1]["baton_ref"] == "refs/remembra/baton/T-1/7"
    outbox = await env.all("SELECT kind, payload FROM crew_outbox")
    assert [json.loads(o["payload"])["end_reason"] for o in outbox] == ["stalled:billing_error"]
    kinds = sorted(r["kind"] for r in await env.all("SELECT kind FROM crew_inbox_items WHERE state = 'open'"))
    assert kinds == ["baton_available", "baton_reserved"]
    row = await get_session(env.conn, a.session["id"])
    assert (row["state"], row["end_reason"], row["limit_level"]) == ("ended", "stalled:billing_error", "exhausted")
    # idempotent, and outside the window the old answer stands
    again = await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    assert again["already"] is True and len(await env.events(CREW, types=("session.quota_blocked",))) == 1
    env.clock.advance(600)
    from remembra.crew.sessions import SessionError

    with pytest.raises(SessionError) as err:
        await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    assert err.value.status == 409


# ---------------------------------------------------------------------------
# D14 classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "message", "kind"),
    [
        ("billing_error", "Credit balance is too low", "quota"),
        ("rate_limit", "You've hit your session limit · resets 5pm", "quota"),
        ("rate_limit", "You’re out of usage credits · add more", "quota"),
        ("rate_limit", "Your org is out of usage · ask an admin", "quota"),
        ("rate_limit", "You've used 100% of your weekly limit", "quota"),
        ("rate_limit", "API Error: Request rejected (429) · This request would exceed your rate limit.", "checkpoint"),
        ("rate_limit", "Server is temporarily limiting requests (not your usage limit)", "checkpoint"),
        ("rate_limit", None, "checkpoint"),
        ("authentication_failed", "Invalid API key", "auth"),
        ("oauth_org_not_allowed", "", "auth"),
        ("detected_limit", None, "quota"),
        ("server_error", "529 Overloaded", "checkpoint"),
        ("unknown", "Unable to connect", "checkpoint"),
    ],
)
def test_stop_failure_classification_follows_d14(error, message, kind):
    assert classify_stop_failure(error, message) == kind


async def test_a_transient_429_checkpoints_only(env):
    a, _zone, task, claim = await _holder(env)
    payload = json.loads((CAPTURES / "mock/stopfailure_ratelimit/03-StopFailure.json").read_text())["payload"]
    res = await env.svc.stall(
        a.session, error="rate_limit", facts={}, baton_ref=None, last_assistant_message=payload["last_assistant_message"]
    )
    assert res["state"] == "active" and res["checkpoint_id"]
    assert await env.events(CREW, types=("session.quota_blocked",)) == []
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "active"
    assert (await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,)))["status"] == "in_progress"
    assert await env.all("SELECT kind FROM crew_outbox") == []


async def test_a_usage_limit_429_is_quota(env):
    a, _zone, task, claim = await _holder(env)
    res = await env.svc.stall(
        a.session, error="rate_limit", facts={}, baton_ref=None, last_assistant_message="You've hit your weekly limit"
    )
    assert res["state"] == "quota_blocked" and res["reason"] == "quota"
    ev = (await env.events(CREW, types=("session.quota_blocked",)))[0]
    assert ev["summary"] == "cc-1 out of credits (rate_limit, reported)"


async def test_an_auth_failure_blocks_with_reason_auth_not_out_of_credits(env):
    a, _zone, task, claim = await _holder(env)
    payload = json.loads((CAPTURES / "real/real_stopfailure_model/03-StopFailure.json").read_text())["payload"]
    res = await env.svc.stall(
        a.session, error=payload["error"], facts={}, baton_ref=None, last_assistant_message=payload["last_assistant_message"]
    )
    assert res["state"] == "quota_blocked" and res["reason"] == "auth"
    row = await get_session(env.conn, a.session["id"])
    assert row["state_reason"] == "auth" and row["limit_level"] is None
    ev = (await env.events(CREW, types=("session.quota_blocked",)))[0]
    assert ev["summary"] == "cc-1 blocked: sign-in failed (authentication_failed, reported)"
    assert "credits" not in ev["summary"]
    # the message is used to classify only: never stored
    blob = json.dumps([dict(r) for r in await env.all("SELECT * FROM crew_events")])
    assert "OAuth access token" not in blob


# ---------------------------------------------------------------------------
# SessionEnd: relay close (step 1) then leave (step 4) keeps the baton ref
# ---------------------------------------------------------------------------


async def _relay_close(env, *, token=None, agent_verified=True):
    return await on_relay_close(
        env.log,
        user_id=OWNER,
        project_id=PROJECT,
        agent_id="claude-code",
        client_session_id="s-a",
        agent_verified=agent_verified,
        handoff_id="mem_h1",
        end_reason="other",
        facts={"uncommitted_files": ["src/pos/cart.ts"], "branch": "main"},
        sections={"done": [], "not_done": ["cart rounding"]},
        facts_source="relay-cli:git",
        session_token=token,
    )


async def test_sessionend_relay_close_then_leave_keeps_the_baton_ref(env):
    h, htok = await host(env)
    a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=htok, session_token=None)
    zone = await add_zone(env, CREW, "pos")
    task = await add_task(env, CREW, 1, owner=a.session, zones=[zone])
    claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    # crewd's relay close carries the token, but a hosted session is left by crewd's own /leave
    close = await _relay_close(env, token=a.session_token)
    assert close["session_left"] is False and close["leave_skipped"] == "left_by_crewd"
    res = await env.svc.leave(
        a.session,
        reason="other",
        facts={"uncommitted_files": ["src/pos/cart.ts"]},
        summary=None,
        baton=False,
        baton_ref="refs/remembra/baton/T-1/7",
    )
    assert res["already"] is False
    c = await env.one("SELECT state, reserve_reason, baton_ref FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "reserved", "reserve_reason": "ended_dirty", "baton_ref": "refs/remembra/baton/T-1/7"}
    types = [e["type"] for e in await env.events(CREW)]
    assert "baton.ref_created" in types and types.count("handoff.created") == 1
    kinds = sorted(r["kind"] for r in await env.all("SELECT kind FROM crew_inbox_items"))
    assert kinds == ["baton_available", "baton_reserved"]
    b = await _join(env, "s-b", checkout="fp-b")
    assert [o["baton_ref"] for o in b.batons_offered] == ["refs/remembra/baton/T-1/7"]


async def test_a_leave_after_the_relay_close_ended_the_session_still_attaches_the_baton_ref(env):
    """An MCP-only session (no host) left by a token-proven relay close; the SessionEnd baton ref arrives next."""
    a, _zone, task, claim = await _holder(env)
    close = await _relay_close(env, token=a.session_token)
    assert close["session_left"] is True and close["claims_reserved"] == [claim]
    res = await env.svc.leave(
        a.session, reason="other", facts={}, summary=None, baton=False, baton_ref="refs/remembra/baton/T-1/9"
    )
    assert res["already"] is True and res["baton_ref_applied"] is True
    c = await env.one("SELECT baton_ref FROM crew_claims WHERE id = ?", (claim,))
    assert c["baton_ref"] == "refs/remembra/baton/T-1/9"
    assert [e["payload"]["ref"] for e in await env.events(CREW, types=("baton.ref_created",))] == ["refs/remembra/baton/T-1/9"]
    # the close-out raised the same baton items as the session service
    kinds = sorted(r["kind"] for r in await env.all("SELECT kind FROM crew_inbox_items WHERE state = 'open'"))
    assert kinds == ["baton_available", "baton_reserved"]
    b = await _join(env, "s-b", checkout="fp-b")
    tasks = TaskService(env.log)
    adopted = await tasks.adopt(CREW, task, Caller.for_session(b.session))
    assert adopted.task["owner_session_id"] == b.session["id"]


# ---------------------------------------------------------------------------
# Relay close without the session token
# ---------------------------------------------------------------------------


async def test_a_relay_close_without_the_session_token_never_ends_the_session(env):
    j = await env.svc.join(user_id=OWNER, req=join_req("s-a", verified=True), host_token=None, session_token=None)
    zone = await add_zone(env, CREW, "pos")
    claim = await add_claim(env, CREW, j.session, zone_id=zone)
    for token in (None, "rcs_wrong", "x" * 600):
        res = await _relay_close(env, token=token, agent_verified=False)
        assert res["session_left"] is False and res["leave_skipped"] == "session_token_required"
        assert res["claims_released"] == [] and res["claims_reserved"] == []
    s = await env.one("SELECT state FROM crew_sessions WHERE id = ?", (j.session["id"],))
    c = await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,))
    assert s["state"] == "active" and c["state"] == "active"
    assert [e["type"] for e in await env.events(CREW) if e["type"] != "session.joined"].count("session.left") == 0


# ---------------------------------------------------------------------------
# Stall / leave facts through crew.redact.outbound
# ---------------------------------------------------------------------------

LEAKY = {
    "branch": "main",
    "last_command": {"command": "PGPASSWORD=hunter2pass psql -h db.internal -U admin -c 'select 1'"},
    "stdout": "DATABASE_URL=postgres://admin:hunter2pass@db.internal/prod",
    "cwd": "/Users/dolphy/Projects/secret-client/src",
    "hostname": "manis-macbook-pro.local",
    "notes": "export ANTHROPIC_API_KEY=sk-ant-api03-" + "Q" * 40,
}


def _assert_clean(stored: str) -> None:
    for secret in ("hunter2pass", "manis-macbook-pro", "/Users/dolphy", "sk-ant-api03-", "PGPASSWORD=hunter2"):
        assert secret not in stored, secret


async def test_stall_facts_pass_the_outbound_choke_point(env):
    a, *_ = await _holder(env)
    await env.svc.stall(a.session, error="overloaded", facts=LEAKY, baton_ref=None)
    row = await env.one("SELECT facts FROM crew_checkpoints WHERE session_id = ?", (a.session["id"],))
    _assert_clean(row["facts"])
    facts = json.loads(row["facts"])
    assert facts["last_command"] == {"verb": "psql"} and "stdout" not in facts and "hostname" not in facts
    assert facts["cwd"] == "~/Projects/secret-client/src"


async def test_quota_and_leave_facts_pass_the_outbound_choke_point(env):
    a, *_ = await _holder(env)
    await env.svc.stall(a.session, error="billing_error", facts=LEAKY, baton_ref=None)
    b = await _join(env, "s-b", checkout="fp-b")
    await env.svc.leave(b.session, reason="other", facts=LEAKY, summary=None, baton=False, baton_ref=None)
    stored = json.dumps([dict(r) for r in await env.all("SELECT facts FROM crew_checkpoints")])
    stored += json.dumps([dict(r) for r in await env.all("SELECT payload FROM crew_outbox")])
    stored += json.dumps([dict(r) for r in await env.all("SELECT payload FROM crew_events")])
    _assert_clean(stored)


async def test_the_redaction_corpus_holds_for_stall_and_checkpoint_facts_through_the_session_service(env):
    corpus = load("redaction/corpus.json")
    cases = [c for c in corpus["cases"] if c["payload_type"] in ("stall", "checkpoint")]
    assert {c["payload_type"] for c in cases} == {"stall", "checkpoint"}
    a, *_ = await _holder(env)
    for n, case in enumerate(cases):
        facts = case["payload"].get("facts", {}) if case["payload_type"] == "checkpoint" else dict(case["payload"])
        facts = {**facts, "n": n}  # a distinct checkpoint per case
        await env.svc.stall(a.session, error="overloaded", facts=facts, baton_ref=None)
    stored = json.dumps([dict(r) for r in await env.all("SELECT facts FROM crew_checkpoints")], ensure_ascii=False)
    for case in cases:
        for secret in case["must_not_contain"]:
            if secret == corpus["context"]["home"]:
                continue  # the server has no repo root; home paths are generic ~ (checked below)
            assert secret not in stored, (case["id"], secret[:12])
    assert "/Users/mani" not in stored


# ---------------------------------------------------------------------------
# Heartbeat footprints → collisions
# ---------------------------------------------------------------------------


async def test_a_heartbeat_footprint_in_a_held_zone_opens_a_collision(env):
    from remembra.crew.collisions import heartbeat_sink

    assert heartbeat_sink in FOOTPRINT_SINKS
    h, htok = await host(env)
    a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=htok, session_token=None)
    b = await env.svc.join(
        user_id=OWNER, req=join_req("s-b", host_id=h["id"], checkout="fp-b"), host_token=htok, session_token=None
    )
    zone = await add_zone(env, CREW, "pos")
    claim = await add_claim(env, CREW, a.session, zone_id=zone)
    fp = [{"path": "src/pos/cart.ts", "state": "dirty", "attribution": "certain", "claim_epoch": None}]
    await env.svc.heartbeat(
        user_id=OWNER,
        host=h,
        body={"sessions": [hb_item(a.session, a.session_token), hb_item(b.session, b.session_token, footprints=fp)]},
    )
    cols = await env.all("SELECT kind, session_a, session_b, claim_id, attribution FROM crew_collisions")
    assert cols == [
        {
            "kind": "exclusive_breach",
            "session_a": b.session["id"],
            "session_b": a.session["id"],
            "claim_id": claim,
            "attribution": "certain",
        }
    ]
    detected = await env.events(CREW, types=("collision.detected",))
    assert len(detected) == 1 and detected[0]["payload"]["collision"]["kind"] == "exclusive_breach"
    touches = await env.one("SELECT touches, zone_ids FROM crew_footprints WHERE session_id = ?", (b.session["id"],))
    assert touches["touches"] == 1 and json.loads(touches["zone_ids"]) == [zone]
    # a write under an older epoch after the holder's epoch moved on is a stale_epoch_write
    await env.conn.execute("UPDATE crew_claims SET epoch = 3 WHERE id = ?", (claim,))
    await env.conn.commit()
    stale = [{"path": "src/pos/tender.ts", "state": "dirty", "attribution": "probable", "claim_epoch": 1}]
    await env.svc.heartbeat(user_id=OWNER, host=h, body={"sessions": [hb_item(b.session, b.session_token, footprints=stale)]})
    kinds = sorted(r["kind"] for r in await env.all("SELECT kind FROM crew_collisions"))
    assert kinds == ["exclusive_breach", "exclusive_breach", "stale_epoch_write"]


async def test_a_huge_activity_age_does_not_fail_the_heartbeat_batch(env):
    h, htok = await host(env)
    a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=htok, session_token=None)
    b = await env.svc.join(
        user_id=OWNER, req=join_req("s-b", host_id=h["id"], checkout="fp-b"), host_token=htok, session_token=None
    )
    res = await env.svc.heartbeat(
        user_id=OWNER,
        host=h,
        body={"sessions": [hb_item(a.session, a.session_token, age=10**11), hb_item(b.session, b.session_token)]},
    )
    assert set(res["per_session"]) == {a.session["id"], b.session["id"]}
    assert "error" not in res["per_session"][b.session["id"]]


# ---------------------------------------------------------------------------
# Queue promotion on every ending path
# ---------------------------------------------------------------------------


async def _queued_behind(env, holder, zone):
    b = await _join(env, "s-b", checkout="fp-b")
    out = await C.request_claim(CrewOps(env.log), CREW, Principal.for_session(b.session), zone_id=zone, wait=True)
    assert out.status == "queued"
    return b, out.claim["id"]


async def _state(env, claim_id):
    return (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim_id,)))["state"]


async def test_a_clean_leave_promotes_the_queue(env):
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "pos")
    await add_claim(env, CREW, a.session, zone_id=zone)
    b, queued = await _queued_behind(env, a, zone)
    await env.svc.leave(a.session, reason="logout", facts={"branch": "main"}, summary=None, baton=False, baton_ref=None)
    assert await _state(env, queued) == "active"
    granted = await env.events(CREW, types=("claim.granted",))
    assert granted[-1]["payload"]["claim"]["id"] == queued
    verdict = await C.server_guard(CrewOps(env.log), CREW, Principal.for_session(b.session), op="write", paths=["src/pos/a.ts"])
    assert verdict["decision"] == "allow" and verdict["rule"] == 7


async def test_a_task_cancel_promotes_the_queue(env):
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "pos")
    task = await add_task(env, CREW, 1, owner=a.session, zones=[zone], status="claimed")
    await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    _b, queued = await _queued_behind(env, a, zone)
    version = (await env.one("SELECT version FROM crew_tasks WHERE id = ?", (task,)))["version"]
    await TaskService(env.log).cancel(CREW, task, Caller.for_session(a.session), if_match=version)
    assert await _state(env, queued) == "active"


async def test_human_release_all_promotes_the_queue(env):
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "pos")
    await add_claim(env, CREW, a.session, zone_id=zone)
    _b, queued = await _queued_behind(env, a, zone)
    await env.svc.release_all(a.session, human_user_id=OWNER, reason="stuck")
    assert await _state(env, queued) == "active"


async def test_a_reservation_expiring_in_the_reaper_promotes_the_queue(env):
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "pos")
    await add_claim(env, CREW, a.session, zone_id=zone)
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)  # non-task: reserve_ttl applies
    _b, queued = await _queued_behind(env, a, zone)
    # the reservation's reserve_ttl has run out (without aging the waiting session itself)
    await env.conn.execute("UPDATE crew_claims SET reserve_expires_at = '2026-09-25T19:00:00.000Z' WHERE state = 'reserved'")
    await env.conn.commit()
    report = await CrewReaper(env.svc).sweep()
    assert report.reservations_expired == 1
    assert await _state(env, queued) == "active"


async def test_a_queued_claim_whose_blocker_ended_elsewhere_is_granted_on_retry(env):
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "pos")
    blocker = await add_claim(env, CREW, a.session, zone_id=zone)
    b, queued = await _queued_behind(env, a, zone)
    # some path ended the blocker without running the queue
    await env.conn.execute("UPDATE crew_claims SET state = 'released' WHERE id = ?", (blocker,))
    await env.conn.commit()
    again = await C.request_claim(CrewOps(env.log), CREW, Principal.for_session(b.session), zone_id=zone, wait=True)
    assert again.status == "granted" and again.claim["id"] == queued


# ---------------------------------------------------------------------------
# Blocked tasks, micro-leases, released batons
# ---------------------------------------------------------------------------


async def test_a_blocked_task_stalls_with_a_report_and_can_be_adopted(env):
    a, _zone, task, claim = await _holder(env, status="blocked")
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    t = await env.one("SELECT status, status_before_stall, current_report_id FROM crew_tasks WHERE id = ?", (task,))
    assert t["status"] == "stalled" and t["status_before_stall"] == "blocked" and t["current_report_id"]
    b = await _join(env, "s-b", checkout="fp-b")
    assert [o["task"] for o in b.batons_offered] == ["T-1"]
    res = await TaskService(env.log).adopt(CREW, task, Caller.for_session(b.session))
    assert res.task["status"] == "claimed" and res.task["owner_session_id"] == b.session["id"]


async def test_a_blocked_task_is_restored_to_blocked_on_recovery(env):
    h, htok = await host(env)
    a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=htok, session_token=None)
    zone = await add_zone(env, CREW, "pos")
    task = await add_task(env, CREW, 1, owner=a.session, zones=[zone], status="blocked")
    await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    env.clock.advance(60)
    await env.svc.heartbeat(user_id=OWNER, host=h, body={"sessions": [hb_item(a.session, a.session_token, age=1)]})
    assert (await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,)))["status"] == "blocked"


async def _micro_lease(env, session):
    cid = await add_claim(env, CREW, session, lease_s=C.MICRO_LEASE_S)
    await env.conn.execute("UPDATE crew_claims SET source = 'micro_lease', path_glob = 'CHANGELOG.md' WHERE id = ?", (cid,))
    await env.conn.commit()
    return cid


async def test_heartbeats_never_extend_a_micro_lease(env):
    h, htok = await host(env)
    a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=htok, session_token=None)
    cid = await _micro_lease(env, a.session)
    before = (await env.one("SELECT lease_expires_at FROM crew_claims WHERE id = ?", (cid,)))["lease_expires_at"]
    for _ in range(20):
        env.clock.advance(60)
        await env.svc.heartbeat(user_id=OWNER, host=h, body={"sessions": [hb_item(a.session, a.session_token)]})
    after = (await env.one("SELECT lease_expires_at FROM crew_claims WHERE id = ?", (cid,)))["lease_expires_at"]
    assert after == before


@pytest.mark.parametrize("how", ["quota", "lost", "dirty_leave"])
async def test_a_micro_lease_ends_instead_of_becoming_a_baton(env, how):
    a = await _join(env, "s-a")
    cid = await _micro_lease(env, a.session)
    if how == "quota":
        await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    elif how == "lost":
        await env.svc.leave(a.session, reason="process_exited", facts={}, summary=None, baton=False, baton_ref=None)
    else:
        await env.svc.leave(a.session, reason="clear", facts={}, summary=None, baton=False, baton_ref=None)
    c = await env.one("SELECT state, reserve_reason FROM crew_claims WHERE id = ?", (cid,))
    assert c["state"] == "expired" and c["reserve_reason"] is None


async def test_a_relay_close_ends_micro_leases(env):
    a = await _join(env, "s-a")
    cid = await _micro_lease(env, a.session)
    res = await _relay_close(env, token=a.session_token)
    assert res["session_left"] is True
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (cid,)))["state"] == "expired"


async def test_a_baton_released_on_purpose_is_offered_and_adoptable_while_the_releaser_lives(env):
    a, _zone, task, claim = await _holder(env)
    await env.conn.execute("UPDATE crew_tasks SET started_at = created_at WHERE id = ?", (task,))
    await env.conn.commit()
    tasks = TaskService(env.log)
    await tasks.release(CREW, task, Caller.for_session(a.session), baton=True)
    c = await env.one("SELECT state, reserve_reason, reserved_for FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "reserved", "reserve_reason": "baton", "reserved_for": None}
    b = await _join(env, "s-b", checkout="fp-b")
    assert [o["task"] for o in b.batons_offered] == ["T-1"]
    assert b.auto_adopted == []  # the releaser is alive: offered, never auto-adopted
    res = await tasks.adopt(CREW, task, Caller.for_session(b.session))
    assert res.task["owner_session_id"] == b.session["id"]
    assert (await env.one("SELECT holder_session_id, state FROM crew_claims WHERE id = ?", (claim,))) == {
        "holder_session_id": b.session["id"],
        "state": "active",
    }


async def test_the_releaser_can_take_its_released_baton_back(env):
    a, _zone, task, claim = await _holder(env)
    await env.conn.execute("UPDATE crew_tasks SET started_at = created_at WHERE id = ?", (task,))
    await env.conn.commit()
    tasks = TaskService(env.log)
    await tasks.release(CREW, task, Caller.for_session(a.session), baton=True)
    res = await tasks.adopt(CREW, task, Caller.for_session(a.session))
    assert res.task["owner_session_id"] == a.session["id"] and res.task["status"] != "stalled"
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "active"


def test_session_tokens_are_sha256_on_both_paths():
    # the close-out check and the claims check hash tokens the same way
    assert C.hash_session_token("rcs_x") == hashlib.sha256(b"rcs_x").hexdigest()


# ---------------------------------------------------------------------------
# Over HTTP: the stall route classifies with last_assistant_message; agent-scoped keys are bound
# ---------------------------------------------------------------------------


async def test_stall_route_classifies_a_429_and_binds_agent_scoped_keys(tmp_path):
    from remembra.auth.rbac import Role
    from remembra.crew.sessions import SESSION_TOKEN_HEADER
    from tests.crew.test_crew_sessions_api import _join_body, _owner
    from tests.crew.test_crew_sessions_api import crew_http as sessions_http

    async with sessions_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        joined = await h.client.post("/api/v1/crews/join", json=_join_body("s-1"), headers=key)
        assert joined.status_code in (200, 201), joined.text
        sid, token = joined.json()["session_id"], joined.json()["session_token"]
        # a codex-scoped key carrying the claude-code session's token is refused
        scoped = await h.keys.create_key(user_id=uid, name="codex", agent_id="codex")
        await h.roles.assign_role(scoped.id, Role("editor"))
        body = {"error": "billing_error", "facts": {}}
        res = await h.client.post(
            f"/api/v1/sessions/{sid}/stall", json=body, headers={"X-API-Key": scoped.key, SESSION_TOKEN_HEADER: token}
        )
        assert res.status_code == 401, res.text
        res = await h.client.post(
            f"/api/v1/sessions/{sid}/leave",
            json={"reason": "other", "facts": {}},
            headers={"X-API-Key": scoped.key, SESSION_TOKEN_HEADER: token},
        )
        assert res.status_code == 401, res.text
        # a transient 429 checkpoints only; a usage-limit 429 blocks
        transient = {
            "error": "rate_limit",
            "facts": {},
            "last_assistant_message": "API Error: Request rejected (429) · This request would exceed your rate limit.",
        }
        res = await h.client.post(f"/api/v1/sessions/{sid}/stall", json=transient, headers={**key, SESSION_TOKEN_HEADER: token})
        assert res.status_code == 200 and res.json()["state"] == "active" and res.json()["checkpoint_id"], res.text
        limit = {**transient, "last_assistant_message": "You've hit your session limit · resets 7pm"}
        res = await h.client.post(f"/api/v1/sessions/{sid}/stall", json=limit, headers={**key, SESSION_TOKEN_HEADER: token})
        assert res.status_code == 200 and res.json()["state"] == "quota_blocked", res.text
        stored = json.dumps([dict(r) for r in await db.fetchall("SELECT * FROM crew_checkpoints")])
        assert "resets 7pm" not in stored and "Request rejected" not in stored
