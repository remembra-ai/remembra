"""The report gate (§5.6, §13.1 "Report gate"): criterion kinds, source labels, strict reports, deploy gate,
live checks, waivers, review, the one-current-report invariant and the nightly invariant job."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remembra.crew.checkpoints import CheckpointService
from remembra.crew.events import ingest_client_events
from remembra.crew.livecheck import LiveChecker
from remembra.crew.reports import (
    ReportService,
    check_report_invariant,
    enforce_report_invariant,
    receipt_seal,
    report_invariant_loop,
)
from remembra.crew.store import now_iso
from remembra.crew.tasks import Caller, CrewServiceError, TaskService
from tests.crew.test_livecheck import HOSTS, resolver_for, tls_server
from tests.crew.wp6_support import (
    CREW,
    event_log,
    events_of,
    open_db,
    seed_crew,
    seed_session,
    seed_zone,
    set_settings,
    valid_envelopes,
)

HUMAN = Caller.for_human("u_owner", privileged=True)
TEST_CRIT = {"id": "c1", "text": "POS tests pass", "kind": "test", "match": "npm test -- pos", "required": True}


class Env:
    def __init__(self, db, factory=None):
        self.db = db
        self.log = event_log(db)
        self.checkpoints = CheckpointService(self.log)
        self.tasks = TaskService(self.log, on_transition=self.checkpoints.on_task_transition)
        self.calls: list[list[str]] = []

        def default_factory(hosts):
            self.calls.append(list(hosts))
            return LiveChecker(hosts)

        self.reports = ReportService(self.log, self.tasks, live_checker=factory or default_factory)

    async def task(self, *, acceptance=(TEST_CRIT,), zone_ids=(), **kw):
        body = {"title": "POS split tender", "zone_ids": list(zone_ids), "acceptance": list(acceptance), "depends_on": [], **kw}
        return (await self.tasks.create(CREW, HUMAN, body)).task

    async def started(self, session, **kw):
        t = await self.task(**kw)
        await self.tasks.start(CREW, t["id"], Caller.for_session(session))
        return t

    async def checkpoint(self, session, facts, trigger="test"):
        return await self.checkpoints.ingest(
            CREW, Caller.for_session(session), {"session_id": session["id"], "trigger": trigger, "facts": facts, "task_id": None}
        )

    async def push(self, session, n=1):
        return await ingest_client_events(
            self.log,
            crew_id=CREW,
            actor=Caller.for_session(session).actor,
            items=[
                {
                    "id": f"push-{session['id']}-{n}",
                    "type": "activity.push",
                    "age_s": 0,
                    "payload": {"upstream": "origin/main", "count": 1, "default_branch": True, "head": "a1b2c3d4e5f6"},
                }
            ],
        )

    async def report(self, session, task, **body):
        return await self.reports.submit(CREW, task["id"], Caller.for_session(session), body)


PASS = {"tests": [{"command": "npm test -- pos", "passed": 12, "failed": 0}], "commits": [{"sha": "a1b2c3d4e5f6"}]}
FAIL = {"tests": [{"command": "npm test -- pos", "passed": 10, "failed": 2}]}


@pytest.fixture()
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    try:
        yield Env(db)
    finally:
        await db.close()


async def test_observed_tests_and_push_auto_accept_and_finish(env):
    s = await seed_session(env.db)
    z = await seed_zone(env.db)
    t = await env.started(s, zone_ids=[z])
    dependent = await env.task(title="after", depends_on=[t["id"]], acceptance=[])
    await env.checkpoint(s, PASS)
    await env.push(s)
    res = await env.report(s, t, sections={"done": ["split tender"]}, summary="Split tender works; tests pass and pushed.")
    assert res.outcome == "accepted"
    assert res.report["kind"] == "completion" and res.report["verdict"] == "complete" and res.report["review_state"] == "accepted"
    assert res.report["criteria"] == [{"id": "c1", "status": "met", "source": "relay-cli"}]
    assert res.gate.seal == "tests ✓ (observed) · pushed ✓ (observed)"
    assert res.report["seal"] == res.gate.seal
    assert res.report["grounding"]["status"] == "consistent"
    assert res.task["status"] == "done" and res.task["current_report_id"] == res.report["id"]
    claim = await env.db.fetchone("SELECT state FROM crew_claims")
    assert claim["state"] == "released"  # finishing releases
    assert (await env.tasks.get(CREW, dependent["id"]))["status"] == "ready"
    evs = await events_of(env.db)
    valid_envelopes(evs)
    done = [e for e in evs if e["type"] == "task.done"]
    assert len(done) == 1 and done[0]["moment"] is True and done[0]["payload"]["report_id"] == res.report["id"]
    kinds = [e["type"] for e in evs]
    assert kinds.index("report.submitted") < kinds.index("report.accepted") < kinds.index("task.done")
    assert await check_report_invariant(env.db.conn) == []


async def test_mcp_sessions_are_self_reported_and_go_to_review_under_strict_reports(env):
    s = await seed_session(env.db, client_kind="mcp", adapter="mcp")
    t = await env.started(s)
    res = await env.report(
        s, t, tests=[{"command": "npm test -- pos", "passed": 5, "failed": 0}], criteria_evidence=[{"id": "@pushed", "met": True}]
    )
    assert res.outcome == "review" and res.report["verdict"] == "complete"
    assert res.report["criteria"] == [{"id": "c1", "status": "met", "source": "agent-declared"}]
    assert res.gate.seal == "tests ✓ (self-reported) · pushed ✓ (self-reported)"
    assert res.task["status"] == "review"
    item = await env.db.fetchone("SELECT * FROM crew_inbox_items WHERE kind = 'review_report'")
    assert item["audience"] == "project" and item["state"] == "open" and item["ref_id"] == t["id"]
    # without strict reports the same evidence auto-accepts
    await set_settings(env.db, CREW, strict_reports=False, wip_per_session=3)
    t2 = await env.started(s)
    res2 = await env.report(
        s,
        t2,
        tests=[{"command": "npm test -- pos", "passed": 5, "failed": 0}],
        criteria_evidence=[{"id": "@pushed", "met": True}],
    )
    assert res2.outcome == "accepted"


async def test_latest_failing_run_is_unmet_and_partial(env):
    s = await seed_session(env.db)
    t = await env.started(s)
    await env.checkpoint(s, PASS)
    await env.checkpoint(s, FAIL, trigger="turn")
    await env.push(s)
    res = await env.report(s, t)
    assert res.report["verdict"] == "partial" and res.report["kind"] == "partial" and res.outcome == "review"
    assert res.report["criteria"][0] == {"id": "c1", "status": "unmet", "source": "relay-cli"}
    assert "1 failing test run(s)" in res.gate.reasons and res.gate.seal.startswith("tests ✗")


async def test_a_pass_older_than_the_last_zone_change_is_unknown(env):
    s = await seed_session(env.db)
    z = await seed_zone(env.db)
    t = await env.started(s, zone_ids=[z])
    await env.checkpoint(s, PASS)
    later = now_iso(datetime.now(UTC) + timedelta(seconds=5))
    async with env.db.transaction():
        await env.db.conn.execute(
            "INSERT INTO crew_footprints (crew_id, session_id, path, zone_ids, first_at, last_at) VALUES (?, ?, ?, ?, ?, ?)",
            (CREW, s["id"], "src/app/pos/cart.ts", json.dumps([z]), later, later),
        )
    await env.push(s)
    res = await env.report(s, t)
    assert res.report["criteria"][0]["status"] == "unknown"
    detail = res.report["criteria_detail"][0]
    assert detail["detail"] == "passed before the last change to zone files"
    assert res.report["verdict"] == "partial"


async def test_push_gate_reviewer_and_out_of_zone_files(env):
    s = await seed_session(env.db)
    z = await seed_zone(env.db)
    t = await env.started(s, zone_ids=[z])
    await env.checkpoint(s, {**PASS, "files_changed": ["src/app/pos/split.ts", "src/app/reports/x.ts"]})
    res = await env.report(s, t)
    assert res.report["verdict"] == "partial" and "not pushed" in res.gate.reasons
    assert res.report["out_of_zone_files"] == ["src/app/reports/x.ts"]
    assert res.gate.seal.endswith("pushed ✗")
    # the push arrives; a resubmission supersedes the current report but the reviewer forces review
    await env.push(s)
    await env.tasks.patch(CREW, t["id"], HUMAN, {"reviewer": "u_owner"}, if_match=(await env.tasks.get(CREW, t["id"]))["version"])
    res2 = await env.report(s, t, summary="second")
    assert res2.report["verdict"] == "complete" and res2.outcome == "review"
    rows = await env.db.fetchall(
        "SELECT id, is_current, superseded_reason FROM crew_reports WHERE task_id = ? ORDER BY created_at", (t["id"],)
    )
    assert [(r["is_current"], r["superseded_reason"]) for r in rows] == [(0, "replaced"), (1, None)]
    assert await check_report_invariant(env.db.conn) == []


async def test_commit_file_manual_criteria_and_declared_evidence(env):
    s = await seed_session(env.db)
    await set_settings(env.db, CREW, deploy_gate={"require_pushed": False, "require_live_check": False})
    t = await env.started(
        s,
        acceptance=[
            {"id": "k1", "text": "commit", "kind": "commit", "match": "a1b2c3d", "required": True},
            {"id": "k2", "text": "file", "kind": "file", "match": "src/app/pos/Receipt.tsx", "required": True},
            {"id": "k3", "text": "manual", "kind": "manual", "required": True},
            {"id": "k4", "text": "optional", "kind": "manual", "required": False},
        ],
    )
    await env.checkpoint(s, {"commits": ["a1b2c3d4e5f6"], "uncommitted_files": ["src/app/pos/Receipt.tsx"]})
    res = await env.report(s, t)
    statuses = {c["id"]: (c["status"], c["source"]) for c in res.report["criteria"]}
    assert statuses == {"k1": ("met", "relay-cli"), "k2": ("met", "relay-cli"), "k3": ("unknown", None), "k4": ("unknown", None)}
    assert res.report["verdict"] == "partial"
    res = await env.report(s, t, criteria_evidence=[{"id": "k3", "met": True, "note": "checked the receipt layout"}])
    statuses = {c["id"]: (c["status"], c["source"]) for c in res.report["criteria"]}
    assert statuses["k3"] == ("met", "agent-declared")
    assert res.report["verdict"] == "complete" and res.outcome == "review"  # agent-declared item under strict_reports


async def test_match_strings_are_never_executed_and_foreign_urls_never_fetched(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    marker = Path(tmp_path) / "pwned-marker"
    s = await seed_session(env.db)
    await set_settings(env.db, CREW, live_check_domains=["yaadbooks.com"])
    t = await env.started(
        s,
        acceptance=[
            {"id": "x", "text": "evil", "kind": "command", "match": "touch pwned-marker", "required": True},
            {
                "id": "y",
                "text": "metadata",
                "kind": "deploy",
                "url": "https://169.254.169.254/latest/meta-data",
                "required": True,
            },
            {"id": "z", "text": "other host", "kind": "deploy", "url": "https://evil.example.net/", "required": True},
        ],
    )
    res = await env.report(s, t)
    assert not marker.exists()
    assert env.calls == []  # no allowed host → the fetcher was never built, nothing fetched
    assert {c["id"]: c["status"] for c in res.report["criteria"]} == {"x": "unknown", "y": "unknown", "z": "unknown"}


async def test_deploy_live_check_is_server_verified(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    try:
        async with tls_server(tmp_path) as (srv, ctx):
            calls: list[str] = []

            def factory(hosts):
                return LiveChecker(
                    hosts,
                    resolver=resolver_for({h: ["127.0.0.1"] for h in HOSTS}, calls),
                    ip_allowed=lambda ip: ip == "127.0.0.1",
                    ssl_context=ctx,
                    connect_port=srv.port,
                )

            env = Env(db, factory)
            await set_settings(
                db,
                CREW,
                live_check_domains=["live.example.com"],
                deploy_gate={"require_pushed": True, "require_live_check": True},
            )
            s = await seed_session(db)
            ok = {"id": "d1", "text": "Live health", "kind": "deploy", "url": "https://live.example.com/ok", "required": True}
            t = await env.started(s, acceptance=[TEST_CRIT, ok])
            await env.checkpoint(s, PASS)
            await env.push(s)
            res = await env.report(s, t)
            assert res.outcome == "accepted", res.gate.reasons
            assert {c["id"]: c["source"] for c in res.report["criteria"]} == {"c1": "relay-cli", "d1": "server-verified"}
            assert res.gate.seal == "tests ✓ (observed) · pushed ✓ (observed) · live ✓ (server-verified)"
            assert res.report["deploy"]["live"][0]["ok"] is True and calls == ["live.example.com"]
            await set_settings(db, CREW, wip_per_session=3)
            bad = {"id": "d1", "text": "Live health", "kind": "deploy", "url": "https://live.example.com/fail", "required": True}
            t2 = await env.started(s, acceptance=[TEST_CRIT, bad])
            await env.checkpoint(s, {**PASS, "n": 2})
            res2 = await env.report(s, t2)
            assert res2.report["criteria"][1]["status"] == "unmet" and res2.report["criteria"][1]["source"] == "server-verified"
            assert "no passing server live check" in res2.gate.reasons and res2.gate.seal.endswith("live ✗")
    finally:
        await db.close()


async def test_replay_owner_and_status_rules(env):
    s = await seed_session(env.db)
    other = await seed_session(env.db, callsign="cc-2")
    t = await env.started(s)
    first = await env.report(s, t, summary="same")
    again = await env.report(s, t, summary="same")
    assert again.outcome == "replay" and again.report["id"] == first.report["id"]
    assert (await env.db.fetchone("SELECT COUNT(*) AS n FROM crew_reports"))["n"] == 1
    with pytest.raises(CrewServiceError) as e:
        await env.report(other, t)
    assert (e.value.status, e.value.error) == (403, "not_task_owner")
    with pytest.raises(CrewServiceError) as e:
        await env.reports.submit(CREW, t["id"], HUMAN, {})
    assert e.value.error == "session_required"
    fresh = await env.task(title="unstarted")
    await set_settings(env.db, CREW, wip_per_session=3)
    await env.tasks.claim(CREW, fresh["id"], Caller.for_session(s))
    with pytest.raises(CrewServiceError) as e:
        await env.report(s, fresh)
    assert (e.value.status, e.value.error) == (409, "invalid_transition")
    with pytest.raises(CrewServiceError) as e:
        await env.report(s, t, sections={"secrets": ["x"]})
    assert e.value.status == 422


async def test_reports_are_redacted(env):
    s = await seed_session(env.db)
    t = await env.started(s)
    res = await env.report(
        s,
        t,
        summary="Deployed with token rem_kcpdSPhvF4t9EPeF5o6rbV3Zb9ZjXAgk",
        sections={"done": ["Set SECRET_KEY=xxcyc3hLb6UQz2LgDcBcjGhPCkSFL2Hixxcyc3hLb6UQ"]},
    )
    blob = json.dumps(res.report)
    assert "rem_kcpdSPhvF4t9EPeF5o6rbV3Zb9ZjXAgk" not in blob and "xxcyc3hLb6UQz2LgDcBcjGhPCkSFL2Hixxcyc3hLb6UQ" not in blob


async def test_review_approve_and_reject_are_human_only(env):
    s = await seed_session(env.db, client_kind="mcp", adapter="mcp")
    t = await env.started(s)
    rep = await env.report(s, t, tests=[{"command": "npm test -- pos", "passed": 5, "failed": 0}])
    with pytest.raises(CrewServiceError) as e:
        await env.reports.review(CREW, t["id"], Caller.for_session(s), "approve", None)
    assert (e.value.status, e.value.error) == (403, "human_only")
    rejected = await env.reports.review(CREW, t["id"], HUMAN, "reject", "missing receipt test")
    assert rejected.outcome == "rejected" and rejected.task["status"] == "in_progress"
    row = await env.db.fetchone("SELECT * FROM crew_reports WHERE id = ?", (rep.report["id"],))
    assert (row["is_current"], row["review_state"], row["reviewed_by"]) == (0, "rejected", "u_owner")
    evs = await events_of(env.db)
    assert any(e["type"] == "report.rejected" and e["moment"] for e in evs)
    rep2 = await env.report(s, t, tests=[{"command": "npm test -- pos", "passed": 6, "failed": 0}])
    assert rep2.outcome == "review"
    approved = await env.reports.review(CREW, t["id"], HUMAN, "approve", "ok")
    assert approved.outcome == "approved" and approved.task["status"] == "done"
    assert (await env.db.fetchone("SELECT state FROM crew_inbox_items WHERE kind = 'review_report'"))["state"] == "resolved"
    with pytest.raises(CrewServiceError) as e:
        await env.reports.review(CREW, t["id"], HUMAN, "approve", None)
    assert e.value.status == 409
    valid_envelopes(await events_of(env.db))
    assert await check_report_invariant(env.db.conn) == []


async def test_waive_one_criterion_then_all(env):
    s = await seed_session(env.db)
    await set_settings(env.db, CREW, deploy_gate={"require_pushed": False, "require_live_check": False})
    manual = {"id": "m1", "text": "Design sign-off", "kind": "manual", "required": True}
    t = await env.started(s, acceptance=[TEST_CRIT, manual])
    await env.checkpoint(s, PASS)
    with pytest.raises(CrewServiceError) as e:
        await env.reports.waive(CREW, t["id"], Caller.for_session(s), "m1", "fine")
    assert e.value.error == "human_only"
    with pytest.raises(CrewServiceError) as e:
        await env.reports.waive(CREW, t["id"], HUMAN, "nope", "fine")
    assert e.value.error == "unknown_criterion"
    await env.reports.waive(CREW, t["id"], HUMAN, "m1", "Mani signed off in person")
    task = await env.tasks.get(CREW, t["id"])
    assert task["acceptance"][1]["waived"]["by"] == "u_owner"
    res = await env.report(s, t)
    assert {c["id"]: c["status"] for c in res.report["criteria"]} == {"c1": "met", "m1": "waived"}
    assert res.outcome == "accepted" and res.gate.seal == "tests ✓ (observed) · manual waived"
    # waive "all" on another task: done without a report (D17)
    await set_settings(env.db, CREW, wip_per_session=3)
    t2 = await env.started(s)
    res2 = await env.reports.waive(CREW, t2["id"], HUMAN, "all", "Shipped by hand")
    assert res2.outcome == "waived" and res2.report["kind"] == "waived" and res2.report["review_state"] == "waived"
    assert res2.task["status"] == "done" and res2.report["summary"] == "Shipped by hand"
    evs = await events_of(env.db)
    overrides = [e for e in evs if e["type"] == "human.override"]
    assert len(overrides) == 2 and all(e["moment"] and e["payload"]["action"] == "waive" for e in overrides)
    assert any(e["type"] == "report.waived" for e in evs)
    valid_envelopes(evs)
    assert await check_report_invariant(env.db.conn) == []


async def test_invariant_job_detects_and_opens_needs_you_once(env):
    s = await seed_session(env.db)
    t = await env.started(s)
    await env.tasks.release(CREW, t["id"], Caller.for_session(s))
    assert await check_report_invariant(env.db.conn) == []
    async with env.db.transaction():  # corrupt: a stalled task with no current report
        await env.db.conn.execute("UPDATE crew_reports SET is_current = 0 WHERE task_id = ?", (t["id"],))
    violations = await enforce_report_invariant(env.log)
    assert violations == [{"task_id": t["id"], "crew_id": CREW, "number": 1, "status": "stalled", "current_reports": 0}]
    await enforce_report_invariant(env.log, CREW)
    items = await env.db.fetchall(
        "SELECT kind, audience, coalesced_count, state FROM crew_inbox_items WHERE kind = 'report_invariant'"
    )
    assert items == [{"kind": "report_invariant", "audience": "project", "coalesced_count": 2, "state": "open"}]


async def test_invariant_loop_runs_on_schedule(env):
    runs: list[float] = []

    async def fake_sleep(seconds):
        runs.append(seconds)
        if len(runs) > 2:
            raise asyncio.CancelledError

    fixed = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
    with pytest.raises(asyncio.CancelledError):
        await report_invariant_loop(env.log, clock=lambda: fixed, sleep=fake_sleep)
    assert runs[0] == 47 * 60  # 03:47 UTC


def test_seal_labels():
    crit = [
        {"kind": "test", "status": "met", "source": "relay-cli"},
        {"kind": "deploy", "status": "met", "source": "server-verified"},
    ]
    assert receipt_seal(crit, True, "relay-cli", True) == "tests ✓ (observed) · pushed ✓ (observed) · live ✓ (server-verified)"
    crit[0]["source"] = "agent-declared"
    assert receipt_seal(crit, False, None, True) == "tests ✓ (self-reported) · pushed ✗ · live ✓ (server-verified)"
    assert receipt_seal([{"kind": "test", "status": "unknown", "source": None}], False, None, False) == "tests ?"
