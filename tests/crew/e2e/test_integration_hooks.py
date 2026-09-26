"""§13.2 integration through the installed hooks (real server, real crewd, real SQLite, real WS; WP-15).

Each test builds its own world (``tests.crew.e2e.harness``): the full crew-mode server, the gate and CLI
installed by ``connect`` into a temp HOME, fake Claude Code / Codex processes running those hooks with
S0-shaped payloads, a webhook receiver and a ``/ws`` subscriber. Covered here (the rest of §13.2 is in
``test_three_agents.py``, ``test_variants.py`` and ``tests/crew/load``):

* the full hook chain ending in a dirty SessionEnd: reserve, baton ref, partial report;
* StopFailure(billing_error) alone: everything within 5 s, including the signed webhook;
* Bash evasion (``python -c`` writing into a held zone): ``exclusive_breach`` (probable), the holder
  notified, and no Stop block text carrying a revert command;
* every tamper form: denied with ``guard.tamper_blocked``; an unrelated settings.json edit is allowed;
* git gates: a merge bringing POS changes is refused at push; a human bypass code lets one push through;
* token bounds: 100 turns with 20 crew changes → at most 20 injections, each ≤600 characters;
* slow (``REMEMBRA_CREW_SLOW=1``): a zones.yml loosening on the default branch stays pending until a
  human approves; a Codex transcript ending in a limit message → ``quota_blocked`` (detected) ≤6 min.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew.crewd import ZONES_S
from tests.crew.e2e.harness import WEBHOOK_URL, FakeAgent, World, git, wait_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
slow = pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_SLOW") != "1", reason="slow: set REMEMBRA_CREW_SLOW=1")


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.close()


def of_type(events: list[dict[str, Any]], etype: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == etype]


def ok(result: dict[str, Any]) -> dict[str, Any]:
    assert not result["denied"], result.get("reason")
    return result


def start_with_task(world: World, agent: FakeAgent, key: str) -> tuple[str, dict[str, Any], str]:
    agent.start()
    crew = str(agent.local_session()["crew_id"])
    pos = next(z["id"] for z in world.snapshot(crew)["zones"] if z["slug"] == "pos")
    with world.human() as h:
        r = h.post(
            f"/crews/{crew}/tasks",
            json={"title": "POS split tender", "zone_ids": [pos], "acceptance": [], "depends_on": []},
            headers={"Idempotency-Key": key},
        )
        assert r.status_code == 201, r.text
    assert "T-1 in_progress" in ok(agent.bash("remembra-crew task start T-1"))["response"]["stdout"]
    return crew, r.json()["task"], pos


def webhook_on(world: World, crew: str) -> None:
    assert world.receiver is not None
    with world.human() as h:
        v = h.get(f"/crews/{crew}").json()["settings_version"]
        r = h.patch(f"/crews/{crew}", json={"settings": {"notify": {"realtime": ["webhook"]}}}, headers={"If-Match": str(v)})
        assert r.status_code == 200, r.text
        r = h.post("/notifications/targets", json={"kind": "webhook", "target": WEBHOOK_URL})
        assert r.status_code == 201, r.text
        world.receiver.secret = r.json()["signing_secret"]


def test_hook_chain_ends_in_a_dirty_session_end(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    crew, task, pos = start_with_task(world, a, "chain-t1")
    feed = world.feed(crew)
    b = world.agent("B", wts["wt-b"])
    b.start()
    assert ok(a.write("src/app/pos/split.ts", "export const split = 1.5;\n"))
    assert b.edit("src/app/pos/split.ts", "[total]", "[1]")["denied"]  # PreToolUse deny for the other session
    ok(a.bash("git add -A && git commit -qm 'pos: split'"))  # PostToolUse commit
    sha = git(wts["wt-a"], "rev-parse", "HEAD")
    ok(a.write("src/app/pos/tender.ts", "export const tender = 3; // wip\n"))
    stop = a.stop()  # Stop: checkpoint, no block
    assert all(not (h.get("stdout") or "").strip() for h in stop if not h.get("async")), stop
    a.end("logout")  # SessionEnd with uncommitted work
    assert wait_for(lambda: of_type(feed.events(), "session.left"), timeout=30), " ".join(feed.types())
    ev = feed.events()
    ref = of_type(ev, "baton.ref_created")[0]["payload"]
    assert (ref["dirty_files"], ref["unpushed"]) == (1, 1), ref
    reserved = [e for e in of_type(ev, "claim.reserved") if e["payload"]["claim"]["zone_id"] == pos]
    assert reserved, " ".join(feed.types())
    assert of_type(ev, "task.stalled") and of_type(ev, "activity.commit") and of_type(ev, "checkpoint.created")
    with world.human() as h:
        reports = h.get(f"/tasks/{task['id']}/reports").json()["reports"]
    [partial] = [r for r in reports if r["kind"] == "partial"]
    assert partial["baton_ref"] == ref["ref"] and sha in partial["commits"], partial
    assert partial["files"] == ["src/app/pos/tender.ts"], partial
    left = of_type(ev, "session.left")[0]["payload"]
    assert left["reason"] == "logout" and left["claims_reserved"] == [reserved[0]["payload"]["claim"]["id"]]


def test_stop_failure_alone_lands_within_5_seconds_with_the_webhook(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    crew, task, pos = start_with_task(world, a, "sf-t1")
    feed = world.feed(crew)
    webhook_on(world, crew)
    ok(a.write("src/app/pos/tender.ts", "export const tender = 4; // wip\n"))
    receiver = world.receiver
    assert receiver is not None
    t0 = time.time()
    a.stop_failure("billing_error", "Credit balance is too low")  # the captured payload, S0 stopfailure_billing

    def landed() -> bool:
        types = set(feed.types())
        need = {"session.quota_blocked", "baton.ref_created", "claim.reserved", "task.stalled", "inbox.item_created"}
        return need <= types and any(any(i["kind"] == "handoff" for i in d["body"]["items"]) for d in receiver.deliveries())

    assert wait_for(landed, timeout=5.0, interval=0.1), (" ".join(feed.types()), receiver.requests)
    assert time.time() - t0 <= 5.0
    [delivery] = [d for d in receiver.deliveries() if any(i["kind"] == "handoff" for i in d["body"]["items"])]
    assert delivery["verified"] and delivery["at"] - t0 <= 5.0
    quota = of_type(feed.events(), "session.quota_blocked")[0]
    assert quota["moment"] and quota["payload"]["source"] == "reported"
    with world.human() as h:
        reports = h.get(f"/tasks/{task['id']}/reports").json()["reports"]
        items = h.get(f"/crews/{crew}/inbox", params={"audience": "project"}).json()["items"]
    assert [r["kind"] for r in reports if r["is_current"]] == ["stalled"]
    assert any(i["kind"] == "baton_available" for i in items), items


def test_bash_evasion_is_detected_without_a_revert_command(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = str(a.local_session()["crew_id"])
    feed = world.feed(crew)
    ok(a.write("src/app/pos/split.ts", "export const split = 9;\n"))  # A holds POS
    b = world.agent("B", wts["wt-b"])
    b.start()
    b_sid = str(b.local_session()["session_id"])
    evasion = b.bash("python3 -c \"open('src/app/pos/x.ts','w').write('export {}')\"")
    assert not evasion["denied"] and (wts["wt-b"] / "src/app/pos/x.ts").exists()  # the gate cannot see inside
    stop = b.stop()
    texts = [h.get("stdout") or "" for h in stop if not h.get("async")]
    assert not any(t.strip() for t in texts) or not any("git checkout" in t or "git restore" in t or "rm " in t for t in texts)
    assert b.cli("renew").returncode == 0

    def breach() -> list[dict[str, Any]]:
        with world.human() as h:
            items = h.get(f"/crews/{crew}/collisions").json()["collisions"]
        return [c for c in items if c["kind"] == "exclusive_breach" and c["session_a"] == b_sid]

    found = wait_for(breach, timeout=20)
    assert found and found[0]["subject"] == "src/app/pos/x.ts" and found[0]["attribution"] == "probable", found
    assert of_type(feed.events(), "collision.detected")
    notified = [e for e in of_type(feed.events(), "inbox.item_created") if e["payload"]["item"]["kind"].startswith("collision")]
    assert notified, [e["payload"]["item"]["kind"] for e in of_type(feed.events(), "inbox.item_created")]


def test_every_tamper_form_is_denied_and_recorded(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = str(a.local_session()["crew_id"])
    feed = world.feed(crew)
    settings = str(world.settings_file)
    forms = {
        "no_verify": a.bash("git commit --no-verify -qm x"),
        "husky_off": a.bash("HUSKY=0 git commit -qm x"),
        "lefthook_off": a.bash("LEFTHOOK=0 git commit -qm x"),
        "hooks_path": a.bash("git -c core.hooksPath=/dev/null commit -qm x"),
        "crewd_kill": a.bash("pkill -f remembra-crewd"),
        "env_crew_var": a.bash("REMEMBRA_BYPASS=abcd git push"),
        "settings_hook_edit": a.edit(settings, "# remembra-crew", ""),
        "crew_policy_write": a.write(".remembra/zones.yml", "version: 1\nzones: {}\n"),
    }
    for name, res in forms.items():
        assert res["denied"] and "BLOCKED by Remembra Crew" in res["reason"], (name, res)
    assert world.crewd_pid() is not None  # crewd still running
    before = Path(settings).read_text()
    unrelated = a.tool(
        "Edit", {"file_path": settings, "old_string": '"hooks": {', "new_string": '"env": {"FOO": "1"},\n  "hooks": {'}
    )
    assert not unrelated["denied"], unrelated.get("reason")
    assert Path(settings).read_text() != before and "# remembra-crew" in Path(settings).read_text()
    assert wait_for(lambda: len(of_type(feed.events(), "guard.tamper_blocked")) >= len(forms), timeout=20), " ".join(feed.types())
    kinds = {e["payload"]["kind"] for e in of_type(feed.events(), "guard.tamper_blocked")}
    assert {"husky_off", "lefthook_off", "hooks_path", "crewd_kill", "env_crew_var", "settings_hook_edit"} <= kinds, kinds
    assert all(e["moment"] for e in of_type(feed.events(), "guard.tamper_blocked"))


def test_merge_then_push_is_refused_and_a_bypass_code_lets_one_push_through(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = str(a.local_session()["crew_id"])
    feed = world.feed(crew)
    ok(a.write("src/app/pos/split.ts", "export const split = 11;\n"))  # A holds POS
    # a human branch with a POS change (a human commit: no session in its process tree)
    env = {**world.base_env()}
    git(wts["main"], "checkout", "-q", "-b", "human-pos", env=env)
    (wts["main"] / "src/app/pos/tender.ts").write_text("export const tender = 'human';\n")
    git(wts["main"], "commit", "-qam", "human pos change", env=env)
    git(wts["main"], "checkout", "-q", "-b", "human-pos2", "main", env=env)
    (wts["main"] / "src/app/pos/split.ts").write_text("export const split = 'human';\n")
    git(wts["main"], "commit", "-qam", "another human pos change", env=env)
    git(wts["main"], "checkout", "-q", "main", env=env)
    b = world.agent("B", wts["wt-b"])
    b.start()
    b_sid = str(b.local_session()["session_id"])
    ok(b.bash("git merge -q --no-edit human-pos"))  # a merge commit: no pre-commit gate runs for merges
    push = ok(b.bash("git push -q origin HEAD:refs/heads/b-work"))
    assert push["ok"] is False and "BLOCKED by Remembra Crew" in push["response"]["stderr"], push["response"]
    assert "refs/heads/b-work" not in git(world.origin, "for-each-ref", "--format=%(refname)")
    # the owner issues a one-time code for B's push; it goes in from the user's own shell inside B's session
    with world.human() as h:
        r = h.post(f"/crews/{crew}/bypass-codes", json={"session_id": b_sid, "scope": "push", "minutes": 15})
        assert r.status_code == 201, r.text
        code = r.json()["code"]
    first = b.run(f"REMEMBRA_BYPASS={code} git push -q origin HEAD:refs/heads/b-work")
    assert first.returncode == 0, first.stderr
    assert "refs/heads/b-work" in git(world.origin, "for-each-ref", "--format=%(refname)")
    ok(b.bash("git merge -q --no-edit human-pos2"))  # more POS changes to push
    again = b.run(f"REMEMBRA_BYPASS={code} git push -q origin HEAD:refs/heads/b-work")
    assert again.returncode != 0, "a bypass code is single use"
    assert "BLOCKED by Remembra Crew" in again.stderr, again.stderr
    assert wait_for(lambda: of_type(feed.events(), "guard.bypass_used"), timeout=15), " ".join(feed.types())
    blocks = [e for e in of_type(feed.events(), "guard.blocked") if e["payload"]["surface"] == "prepush"]
    assert blocks, " ".join(feed.types())


def test_token_bounds_over_a_long_session(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    b = world.agent("B", wts["wt-b"])
    b.start()
    injections: list[str] = []
    changes = 0
    for turn in range(100):
        if turn % 5 == 0 and changes < 20:
            zone = "reports" if changes % 2 == 0 else "pos"
            verb = "claim" if changes % 4 < 2 else "release"
            assert b.cli(verb, zone).returncode in (0, 1)  # a crew change either way (granted, refused, released)
            assert b.cli("say", f"crew change {changes}", "--kind", "note").returncode == 0
            changes += 1
        for h in a.prompt(f"turn {turn}"):
            out = (h.get("stdout") or "").strip()
            if out:
                ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                injections.append(ctx)
    assert changes == 20
    assert len(injections) <= 20, len(injections)
    assert all(len(t) <= 600 for t in injections), [len(t) for t in injections]


@slow
def test_a_loosening_zones_commit_stays_pending_until_a_human_approves(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = str(a.local_session()["crew_id"])
    feed = world.feed(crew)
    # the owner (a plain shell: no session) commits a zones.yml without the pos zone on the default branch
    env = world.base_env()
    zones = wts["main"] / ".remembra" / "zones.yml"
    text = zones.read_text()
    loosened = text.split("  pos:")[0] + "  reports:" + text.split("  reports:")[1]
    zones.write_text(loosened)
    compiled = world.layout.snapshots / f"{crew}.zones.json"
    before = (json.loads(compiled.read_text()) if compiled.exists() else {}).get("sha")
    git(wts["main"], "commit", "-qam", "drop the pos zone", env=env)
    # crewd reads the default branch's zones.yml every ZONES_S (30 s): first it compiles the new file,
    # then it uploads it and the server raises zone.change_pending. Wait on each step, not one fixed
    # deadline, and say which step did not happen (a failed upload is retried on the next tick).
    picked = wait_for(
        lambda: compiled.exists() and json.loads(compiled.read_text()).get("sha") not in (None, before),
        timeout=ZONES_S + 30,
        interval=1,
    )
    status = json.loads(world.layout.status_file.read_text()) if world.layout.status_file.exists() else {}
    assert picked, f"crewd did not compile the committed zones.yml (status {status})"
    assert world.server is not None and world.server.proc is not None and world.server.proc.poll() is None, (
        "the e2e server exited during the test; see server/server.log"
    )
    pending = wait_for(lambda: of_type(feed.events(), "zone.change_pending"), timeout=3 * ZONES_S, interval=1)
    status = json.loads(world.layout.status_file.read_text()) if world.layout.status_file.exists() else {}
    assert pending and pending[0]["payload"]["loosening"] is True, f"{' '.join(feed.types())} (crewd status {status})"
    assert any(z["slug"] == "pos" for z in world.snapshot(crew)["zones"])  # policy unchanged
    b = world.agent("B", wts["wt-b"])
    b.start()
    ok(a.write("src/app/pos/split.ts", "export const split = 12;\n"))
    assert b.write("src/app/pos/split.ts", "x\n")["denied"]  # still enforced while pending
    with world.human() as h:
        r = h.post(f"/zone-changes/{pending[0]['payload']['change_id']}/approve")
        assert r.status_code == 200, r.text
    assert wait_for(lambda: of_type(feed.events(), "zone.change_decided"), timeout=15)
    assert wait_for(lambda: not any(z["slug"] == "pos" for z in world.snapshot(crew)["zones"]), timeout=15)


@slow
def test_codex_transcript_limit_message_is_detected_within_6_minutes(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        wts = w.setup(("wt-a", "wt-b", "wt-c"), "--include-unverified", "--agent", "claude-code", "--agent", "codex")
        x = w.agent("X", wts["wt-a"], adapter="codex")
        x.start()
        crew = str(x.local_session()["crew_id"])
        feed = w.feed(crew)
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        line = {
            "timestamp": now,
            "type": "event_msg",
            "payload": {"type": "error", "message": "You've hit your usage limit. Try again in 3 days."},
        }
        with x.transcript.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        t0 = time.time()
        found = wait_for(lambda: of_type(feed.events(), "session.quota_blocked"), timeout=390, interval=5)
        assert found, " ".join(feed.types())
        assert time.time() - t0 <= 6 * 60
        assert found[0]["payload"]["source"] == "detected" and found[0]["payload"]["error"] == "detected_limit", found
        assert x.proc.poll() is None  # the Codex process is still alive: only the transcript said so
    finally:
        w.close()
