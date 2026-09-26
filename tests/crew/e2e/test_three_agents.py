"""The 3-agent end-to-end, model-free variant (spec §13.3, launch gate; WP-15).

Real processes throughout: the crew-mode API (:mod:`tests.crew.e2e.server`), ``remembra-crewd``,
the vendored gate and the ``remembra-crew`` CLI installed by the real ``connect`` into a temp HOME,
git gates in a real repository with three worktrees, a webhook receiver and a dashboard-style
``/ws`` subscriber. The three "Claude Code" sessions are :class:`FakeAgent` processes that run the
installed hook commands with payloads rebuilt from the S0 captures; they run the tools themselves
(Write, Edit, Bash) exactly when a real Claude Code would, i.e. only when PreToolUse allowed it.

Steps and assertions follow the §13.3 table (1–9). The dashboard's own reducer and lane models are
checked against the same live server by ``test_dashboard_e2e.py``.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import schemas as S
from tests.crew.e2e.harness import VITEST, WEBHOOK_URL, DashboardObserver, World, git, wait_for

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("npm") is None or not VITEST.exists(),
    reason="needs git, npm and the dashboard toolchain (dashboard/node_modules)",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def of_type(events: list[dict[str, Any]], etype: str, **match: Any) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e["type"] != etype:
            continue
        if all(_dig(e, k) == v for k, v in match.items()):
            out.append(e)
    return out


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def event_time(e: dict[str, Any]) -> float:
    return datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00")).timestamp()


def zone_id(snap: dict[str, Any], slug: str) -> str:
    return next(z["id"] for z in snap["zones"] if z["slug"] == slug)


def task_of(snap: dict[str, Any], number: int) -> dict[str, Any]:
    return next(t for t in snap["tasks"] if t["number"] == number)


def claim_of(snap: dict[str, Any], zid: str) -> dict[str, Any] | None:
    live = [c for c in snap["claims"] if c.get("zone_id") == zid and c["state"] in S.LIVE_CLAIM_STATES]
    return live[0] if live else None


def denied_reason(result: dict[str, Any]) -> str:
    assert result["denied"], json.dumps(result)[:2000]
    return str(result["reason"])


def kinds_delivered(receiver: Any, kind: str) -> list[dict[str, Any]]:
    """Verified signed deliveries carrying an item of ``kind`` (a delivery may batch several items)."""
    out = []
    for d in receiver.deliveries():
        if any(i.get("kind") == kind for i in d["body"].get("items") or ()):
            assert d["verified"], d["headers"]
            out.append(d)
    return out


def bash_ok(result: dict[str, Any]) -> dict[str, Any]:
    assert not result["denied"], result.get("reason")
    assert result["ok"], json.dumps(result["response"])[:2000]
    return result


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.close()


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


def test_three_agents_end_to_end(world: World) -> None:
    wts = world.make_repos()
    world.start_server()
    world.connect()
    receiver = world.receiver
    assert receiver is not None

    # ---- 1. A starts, starts T-1, edits split.ts, commits, runs tests, edits tender.ts ------------
    a = world.agent("A", wts["wt-a"])
    brief_a = a.start()
    assert "CREW" in brief_a and "unavailable" not in brief_a, brief_a
    crew = str(a.local_session()["crew_id"])
    a_sid = str(a.local_session()["session_id"])
    assert a.local_session()["callsign"] == "cc-1"
    feed = world.feed(crew)
    dash = DashboardObserver(world.server, crew, world.tmp / "dashboard")  # type: ignore[arg-type]
    world.closers.append(dash.close)

    # Mani's setup (the dashboard, a human JWT): webhook channel, signed webhook target, T-1 and T-2.
    with world.human() as h:
        current = h.get(f"/crews/{crew}").json()
        r = h.patch(
            f"/crews/{crew}",
            json={"settings": {"notify": {"realtime": ["email", "webhook"]}}},
            headers={"If-Match": str(current["settings_version"])},
        )
        assert r.status_code == 200, r.text
        r = h.post("/notifications/targets", json={"kind": "webhook", "target": WEBHOOK_URL})
        assert r.status_code == 201, r.text
        receiver.secret = r.json()["signing_secret"]
        snap = world.snapshot(crew)
        pos, reports = zone_id(snap, "pos"), zone_id(snap, "reports")
        acceptance = [{"id": "c1", "kind": "test", "text": "POS tests pass", "match": "npm test -- pos", "required": True}]
        r = h.post(
            f"/crews/{crew}/tasks",
            json={"title": "POS split tender", "zone_ids": [pos], "acceptance": acceptance, "depends_on": []},
            headers={"Idempotency-Key": "e2e-t1"},
        )
        assert r.status_code == 201, r.text
        t1 = r.json()["task"]
        r = h.post(
            f"/crews/{crew}/tasks",
            json={"title": "Reports export", "zone_ids": [reports], "acceptance": [], "depends_on": []},
            headers={"Idempotency-Key": "e2e-t2"},
        )
        assert r.status_code == 201, r.text
        t2 = r.json()["task"]
    assert (t1["number"], t2["number"]) == (1, 2)

    a.prompt("Implement split tender in POS (T-1).")
    started = bash_ok(a.bash("remembra-crew task start T-1"))
    assert "T-1 in_progress" in started["response"]["stdout"], started["response"]
    assert not a.write("src/app/pos/split.ts", "export const split = (t: number) => [t / 2, t / 2];\n")["denied"]
    bash_ok(a.bash("git add -A && git commit -q -m 'pos: split tender'"))
    tests = bash_ok(a.bash("npm test -- pos"))
    assert "3 passed" in tests["response"]["stdout"]
    assert not a.write("src/app/pos/tender.ts", "export const tender = 2; // half done\n")["denied"]
    a.stop()

    def step1_done() -> bool:
        ev = feed.events()
        return bool(of_type(ev, "activity.commit")) and bool(of_type(ev, "activity.test_verdict_changed"))

    assert wait_for(step1_done, timeout=30), " ".join(feed.types())
    ev = feed.events()
    granted = of_type(ev, "claim.granted", **{"payload.claim.zone_id": pos})
    assert granted and granted[0]["payload"]["claim"]["holder_session_id"] == a_sid, granted
    assert granted[0]["payload"]["claim"]["epoch"] == 1
    assert of_type(ev, "checkpoint.created"), [e["type"] for e in ev]
    snap = world.snapshot(crew)
    assert task_of(snap, 1)["status"] == "in_progress" and task_of(snap, 1)["owner_session_id"] == a_sid
    lane_a = next(s for s in snap["sessions"] if s["id"] == a_sid)
    assert lane_a["state"] == "active", lane_a
    assert claim_of(snap, pos)["holder_session_id"] == a_sid  # the POS chip on A's lane

    # ---- 2. B starts in its own worktree; the brief names cc-1 on POS; B is denied -----------------
    b = world.agent("B", wts["wt-b"])
    brief_b = b.start()
    assert "DO NOT TOUCH: zone pos → cc-1 T-1" in brief_b, brief_b
    b_sid = str(b.local_session()["session_id"])
    b.prompt("Fix rounding in src/app/pos/split.ts")
    before_b = (wts["wt-b"] / "src/app/pos/split.ts").read_text()
    reason = denied_reason(b.edit("src/app/pos/split.ts", "[total]", "[Math.round(total)]"))
    assert "cc-1" in reason and "T-1" in reason, reason
    assert (wts["wt-b"] / "src/app/pos/split.ts").read_text() == before_b

    # 2a. straight into A's checkout
    before_a = (wts["wt-a"] / "src/app/pos/split.ts").read_text()
    denied_reason(b.edit(str(wts["wt-a"] / "src/app/pos/split.ts"), "t / 2", "Math.round(t / 2)"))
    assert (wts["wt-a"] / "src/app/pos/split.ts").read_text() == before_a

    # 2b. tamper: --no-verify and editing the zone policy
    tamper_commit = denied_reason(b.bash("git commit --no-verify -am 'sneak'"))
    tamper_zones = denied_reason(b.write(".remembra/zones.yml", "version: 1\nzones: {}\n"))
    assert "switch off crew protection" in tamper_commit, tamper_commit
    assert "is crew policy" in tamper_zones, tamper_zones
    for text in (reason, tamper_commit, tamper_zones):  # §8.2 contracts: templates only, never a destructive command
        assert not any(bad in text for bad in ("git reset", "git checkout --", "git restore", "rm -", "--no-verify")), text
    assert (wts["wt-b"] / ".remembra/zones.yml").read_text() == (wts["main"] / ".remembra/zones.yml").read_text()

    def step2_done() -> bool:
        ev = feed.events()
        return len(of_type(ev, "guard.tamper_blocked")) >= 2 and bool(of_type(ev, "guard.blocked"))

    assert wait_for(step2_done, timeout=30), " ".join(feed.types())
    ev = feed.events()
    blocked = of_type(ev, "guard.blocked", **{"actor.id": b_sid})
    assert any(e["payload"].get("zone") == "pos" and e["payload"].get("holder") == "cc-1" for e in blocked), blocked
    tampers = of_type(ev, "guard.tamper_blocked")
    assert all(e["moment"] for e in tampers), tampers
    assert wait_for(lambda: kinds_delivered(receiver, "tamper"), timeout=30), receiver.requests
    tamper_alert_at = kinds_delivered(receiver, "tamper")[0]["at"]

    # ---- 3. B picks T-2 and works in reports ---------------------------------------------------------
    bash_ok(b.bash("remembra-crew task start T-2"))
    assert not b.write("src/app/reports/export.ts", "export const exportCsv = () => 'a,b';\n")["denied"]
    assert wait_for(
        lambda: of_type(
            feed.events(), "claim.granted", **{"payload.claim.zone_id": reports, "payload.claim.holder_session_id": b_sid}
        ),
        timeout=15,
    ), " ".join(feed.types())
    assert not of_type(feed.events(), "collision.detected")

    # ---- 4. A's credits run out: the recorded StopFailure(billing_error), then A is killed -------------
    t_stall = time.time()
    # S0 recorded SessionEnd before StopFailure in this run (the order is not stable); replay that order.
    a.end("other")
    a.stop_failure("billing_error", "Credit balance is too low")
    a.kill()

    def stalled() -> list[dict[str, Any]] | None:
        ev = feed.events()
        need = ["session.quota_blocked", "baton.ref_created", "task.stalled", "claim.reserved"]
        return ev if all(of_type(ev, t) for t in need) else None

    ev = wait_for(stalled, timeout=5.0, interval=0.1)
    elapsed = time.time() - t_stall
    assert ev, f"stall not complete within 5 s: {' '.join(feed.types())}"
    quota = of_type(ev, "session.quota_blocked")[0]
    assert quota["actor"]["id"] == a_sid and quota["payload"]["source"] == "reported"
    assert quota["payload"]["error"] == "billing_error" and quota["moment"]
    ref_ev = of_type(ev, "baton.ref_created")[0]
    baton_ref = ref_ev["payload"]["ref"]
    assert ref_ev["payload"]["dirty_files"] >= 1 and baton_ref.startswith("refs/remembra/baton/")
    assert "tender.ts" in git(wts["main"], "show", "--stat", "--format=", baton_ref)
    reserved = of_type(ev, "claim.reserved", **{"payload.claim.zone_id": pos})
    assert reserved and reserved[0]["payload"]["reason"] == "quota", reserved
    snap = world.snapshot(crew)
    assert task_of(snap, 1)["status"] == "stalled"
    with world.human() as h:
        rep = h.get(f"/tasks/{t1['id']}/reports").json()
        stalled_report = next(r for r in rep["reports"] if r["kind"] == "stalled")
        assert stalled_report["baton_ref"] == baton_ref, stalled_report
        assert stalled_report["commits"], json.dumps(stalled_report, indent=1)
        inbox = h.get(f"/crews/{crew}/inbox", params={"audience": "project"}).json()
        assert any(i["kind"] == "baton_available" and i["state"] != "resolved" for i in inbox["items"]), inbox
    handoffs = [e for e in ev if e["type"] == "handoff.created"]
    assert handoffs, [e["type"] for e in ev]
    print(f"stall visible after {elapsed:.2f}s")

    # ---- 5. B tries POS again: the reserved reason, and no adopt line (B was not offered) --------------
    reason = denied_reason(b.edit("src/app/pos/split.ts", "[total]", "[Math.round(total)]"))
    assert "RESERVED for the next pickup of T-1" in reason and "You were not offered this baton" in reason, reason
    assert "adopt" not in reason.lower(), reason

    # ---- 6. C starts in its own worktree: the baton is offered to C -----------------------------------
    c = world.agent("C", wts["wt-c"])
    brief_c = c.start()
    c_sid = str(c.local_session()["session_id"])
    assert "YOUR BATON (offered to you): T-1" in brief_c, brief_c
    assert "remembra-crew adopt T-1" in brief_c, brief_c
    assert f"1 dirty file saved as {baton_ref}" in brief_c and "1 commit unpushed" in brief_c, brief_c
    assert "billing_error, reported" in brief_c, brief_c
    assert "DO NOT TOUCH: zone reports → cc-2" in brief_c, brief_c
    head, _, data = brief_c.partition(S.DATA_OPEN)
    assert "POS split tender" not in head and "POS split tender" in data, brief_c  # agent text only in the data block
    assert wait_for(lambda: of_type(feed.events(), "claim.offered_in_brief"), timeout=10)

    # ---- 7. C adopts, continues, commits, pushes, reports -----------------------------------------------
    c.prompt("Continue T-1 (the baton offered to you).")
    adopted = bash_ok(c.bash("remembra-crew adopt T-1"))
    out = adopted["response"]["stdout"]
    assert out.startswith("ADOPTED T-1") and "Work restored from" in out, out
    assert (wts["wt-c"] / "src/app/pos/tender.ts").read_text() == "export const tender = 2; // half done\n"
    # §5.4: stalled ─adopt─▶ claimed; continuing the work is ``start`` (claimed ─▶ in_progress).
    assert "T-1 in_progress" in bash_ok(c.bash("remembra-crew task start T-1"))["response"]["stdout"]
    assert not c.edit("src/app/pos/tender.ts", "// half done", "// done")["denied"]
    bash_ok(c.bash("git add -A && git commit -q -m 'pos: finish tender'"))
    bash_ok(c.bash("npm test -- pos"))
    bash_ok(c.bash("git push -q -u origin HEAD"))
    reported = bash_ok(c.bash("remembra-crew report T-1 --done 'split tender finished'"))
    assert reported["response"]["stdout"].startswith("REPORT"), reported["response"]

    assert wait_for(lambda: of_type(feed.events(), "task.done"), timeout=20), " ".join(feed.types())
    ev = feed.events()
    passed = of_type(ev, "baton.passed")
    assert passed, [e["type"] for e in ev]
    bp = passed[-1]["payload"]
    assert (bp["from_session"], bp["to_session"], bp["kind"], bp["baton_ref"]) == (a_sid, c_sid, "adopt", baton_ref), bp
    # baton.passed is emitted when the server authorises the adopt, before crewd restores the ref; crewd then
    # reports the outcome: baton.restored{restored: true} for that pass, and crew_batons.restored = 1 (§13.3 step 7)
    restored_ev = of_type(ev, "baton.restored")
    assert restored_ev, [e["type"] for e in ev]
    rp = restored_ev[-1]["payload"]
    assert (rp["baton_id"], rp["restored"], rp["status"], rp["to_session"]) == (bp["baton_id"], True, "restored", c_sid), rp
    assert rp["files"] >= 1 and rp["baton_ref"] == baton_ref, rp
    with world.human() as h:
        batons = h.get(f"/crews/{crew}/batons").json()["batons"]
    assert next(b for b in batons if b["id"] == bp["baton_id"])["restored"] is True, batons
    adopted_ev = of_type(ev, "claim.adopted", **{"payload.claim.zone_id": pos})
    assert adopted_ev and adopted_ev[-1]["payload"]["claim"]["epoch"] == 2, adopted_ev
    assert not of_type(ev, "guard.blocked", **{"actor.id": c_sid})
    snap = world.snapshot(crew)
    assert claim_of(snap, pos) is None  # POS released with the completion
    with world.human() as h:
        reports_t1 = h.get(f"/tasks/{t1['id']}/reports").json()["reports"]
    current = [r for r in reports_t1 if r.get("is_current")]
    assert len(current) == 1 and current[0]["verdict"] == "complete", json.dumps(reports_t1, indent=1)
    labels = json.dumps(current[0])
    assert "observed" in labels, labels

    # ---- 8. the feed: seq order, no gaps, everything above, live within 5 s ------------------------------
    final = world.events(crew)
    head_seq = int(final[-1]["seq"])
    assert wait_for(lambda: bool(feed.arrivals) and max(feed.arrivals) >= head_seq, timeout=10)
    seqs = [e["seq"] for e in feed.events()]
    assert seqs == sorted(seqs) and seqs == list(range(1, head_seq + 1)), seqs[:50]
    wanted = {
        "claim.granted", "activity.commit", "checkpoint.created", "guard.blocked", "guard.tamper_blocked",
        "session.quota_blocked", "baton.ref_created", "claim.reserved", "task.stalled", "claim.offered_in_brief",
        "baton.passed", "claim.adopted", "task.done",
    }  # fmt: skip
    assert wanted <= set(feed.types()), wanted - set(feed.types())
    live_lat = [feed.arrivals[e["seq"]] - event_time(e) for e in feed.events() if event_time(e) >= feed.subscribed_at]
    assert len(live_lat) > 30, len(live_lat)
    assert max(live_lat) <= 5.0, max(live_lat)
    assert of_type(final, "inbox.item_resolved"), "the baton_available Needs-you item was not resolved"

    # The dashboard (its own store, reducer and view models, over the same socket a browser uses).
    report = dash.finish(head_seq)
    ms = report["milestones"]
    for name in (
        "t1_in_progress_by_a",
        "blocked_b",
        "tamper",
        "pickup_slot",
        "needs_you_open",
        "baton_passed",
        "t1_done",
        "needs_you_resolved",
    ):
        assert name in ms, (
            name,
            ms,
            report.get("inbox"),
            report.get("inbox_reloaded"),
            [(e["payload"]["item"]["kind"], e["type"]) for e in final if e["type"].startswith("inbox.")],
        )
        ts = report["event_ts"][str(ms[name]["seq"])]
        shown = (ms[name]["at"] / 1000.0) - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        assert shown <= 5.0, (name, shown)  # visible on the dashboard ≤5 s after the server event
    assert "pos:active" in report["lane_a"]["claims"], report["lane_a"]
    assert any(row.startswith("cc-1:T-1:POS ▨ excl") for row in report["site_board_step1"]["loose"]), report["site_board_step1"]
    slot = report["slot"]
    assert (slot["from"], slot["reason"], slot["ref"]) == ("cc-1", "quota", baton_ref), slot
    assert slot["saved"].startswith("1 uncommitted file saved."), slot
    assert slot["offeredTo"] == ["cc-3"], slot
    assert report["baton_text"].startswith("Baton passed cc-1 → cc-3"), report["baton_text"]
    assert (report["pass_plan_motion"], report["pass_plan_reduced"]) == ("animate", "instant")
    assert report["reloaded"] == report["live"]  # a reload reproduces the same state (also asserted in vitest)
    assert report["live"]["last_seq"] >= head_seq

    # ---- 9. the record: trail, reports, agent lineage, recall ------------------------------------------
    with world.human() as h:
        trail = h.get("/trail", params={"project_id": snap["crew"]["project_id"]}).json()
        agent_page = h.get(f"/crews/{crew}/agents/claude-code").json()
        reports_t1 = h.get(f"/tasks/{t1['id']}/reports").json()["reports"]
    kinds = [(r["kind"], bool(r.get("is_current"))) for r in reports_t1]
    assert ("stalled", False) in kinds and ("completion", True) in kinds, kinds
    assert "T-1" in json.dumps(trail), json.dumps(trail)[:2000]
    assert c_sid in json.dumps(agent_page) and a_sid in json.dumps(agent_page), json.dumps(agent_page)[:2000]
    with world.api() as api:
        recall = api.post(
            "/memories/recall", json={"query": "what happened on T-1", "project_id": snap["crew"]["project_id"]}
        ).json()
    assert "T-1" in json.dumps(recall), json.dumps(recall)[:2000]

    # ---- the real-time handoff alert (step 4) -----------------------------------------------------
    # §9.11 batches real-time alerts per target in a 2-minute window: the tamper alert of step 2b opened
    # one, so the quota handoff goes out when it closes (alone, §13.2 asserts it within 5 s).
    handoff = wait_for(lambda: kinds_delivered(receiver, "handoff"), timeout=max(5.0, tamper_alert_at + 130 - time.time()))
    assert handoff, receiver.requests
    sent = handoff[0]["at"]
    assert sent <= max(t_stall + 5.0, tamper_alert_at + 120 + 10), (sent - t_stall, sent - tamper_alert_at)
    item = next(i for i in handoff[0]["body"]["items"] if i["kind"] == "handoff")
    assert item["text"] == "cc-1 stopped (billing_error, reported). Work saved; zone pos reserved for the next pickup.", item
    assert item["link"].endswith(f"view=feed&seq={quota['seq']}"), item
    assert handoff[0]["headers"].get("Host") == "hooks.e2e.test"
