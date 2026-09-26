"""Focused integration checks for the three defects the 3-agent E2E found (WP-15, §13.2).

Each runs the real crew routers over a real ``crew.db`` (auth on, real API key) with an in-process
``Crewd`` (``tests.crew.wp9_support``), so a regression points at one seam instead of a 2-minute run:

1. ``POST /crews/{id}/events`` exists: crewd's client events (commits, pushes, test verdicts, guard and
   tamper blocks, gate errors, missing git hooks) reach the event log. Before WP-15 the route was in the
   contract (WP-2) but no router served it, so every client event sat in crewd's outbox for ever and the
   report gate never saw an observed commit, push or test.
2. A stall's facts carry every commit the session made, so the stalled report lists the work (§13.3 step 4).
3. Adopting a stalled task resolves the Needs-you ``baton_available`` and crew ``baton_reserved`` items the
   stall raised (§13.3 step 8 "Needs-you resolved").
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator

import httpx
import pytest

from remembra.crew import schemas as S
from remembra.relay.crew import baton as B
from remembra.relay.crew.gate import Layout
from tests.crew.wp9_support import add_worktree, crew_server, git, make_repo, new_crewd, peer

HEADER = "X-Remembra-Crew-Session"


@pytest.fixture
def agents() -> Iterator[list[int]]:
    procs = [subprocess.Popen(["sleep", "600"]) for _ in range(2)]
    yield [p.pid for p in procs]
    for p in procs:
        p.kill()
        p.wait()


def _join(sid: str, cwd: object, pid: int) -> dict[str, object]:
    return {"adapter": "claude-code", "agent_id": "claude-code", "client_session_id": sid, "cwd": str(cwd), "agent_pid": pid}


async def test_client_events_route_accepts_the_whitelist_only(tmp_path, agents):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive=set(agents))
        a = await d.join(peer(agents[0]), _join("sess-a", repo, agents[0]))
        crew_id, token = str(a["crew_id"]), d.tokens[str(a["key"])]
        key = {"X-API-Key": srv.key}
        async with httpx.AsyncClient(transport=srv.transport(), base_url="http://test") as http:
            url = f"/api/v1/crews/{crew_id}/events"
            commit = {
                "id": "evt-commit-1",
                "type": "activity.commit",
                "age_s": 2,
                "payload": {"sha": "a" * 40, "subject_hash": "b" * 32, "files": ["src/app/pos/x.ts"]},
            }
            forged = {"id": "evt-forged-1", "type": "baton.passed", "age_s": 0, "payload": {}}
            block = {
                "id": "evt-block-1",
                "type": "guard.blocked",
                "age_s": 0,
                "payload": {"path_rel": "src/app/pos/x.ts", "zone": "pos", "holder": "cc-2", "rule": 9, "op": "write",
                            "decision": "deny", "surface": "pretool", "coalesced": 1},
            }  # fmt: skip
            block2 = {**block, "id": "evt-block-2", "payload": {**block["payload"], "surface": "precommit"}}

            assert (await http.post(url, json={"events": [commit]}, headers=key)).status_code == 403  # no session token
            r = await http.post(url, json={"events": [commit]}, headers={**key, HEADER: "rcs_forged"})
            assert r.status_code == 401, r.text
            r = await http.post(url, json={"events": [commit, forged, block, block2]}, headers={**key, HEADER: token})
            assert r.status_code == 200, r.text
            results = {x["id"]: x for x in r.json()["results"]}
            assert results["evt-commit-1"]["status"] == "accepted" and results["evt-commit-1"]["seq"]
            assert results["evt-forged-1"]["status"] == "rejected"  # server-only type from a client
            assert results["evt-block-1"]["status"] == "accepted"
            assert results["evt-block-2"]["status"] == "coalesced"  # guard.blocked per zone, 5 min
            again = await http.post(url, json={"events": [commit]}, headers={**key, HEADER: token})
            assert again.json()["results"][0]["status"] == "duplicate"  # the item id is the idempotency key
            assert (await http.post(url, json={"events": "nope"}, headers={**key, HEADER: token})).status_code == 422

        rows = await srv.rows(
            "SELECT type, origin, session_id, payload FROM crew_events WHERE crew_id = ? AND origin = 'client'", (crew_id,)
        )
        assert [(r["type"], r["session_id"]) for r in rows] == [
            ("activity.commit", a["session_id"]),
            ("guard.blocked", a["session_id"]),
        ]

        # crewd's own outbox path now delivers (and empties the outbox)
        (repo / "src/app/reports/export.ts").write_text("export const x = 9;\n")
        git(repo, "commit", "-qam", "reports")
        await d.on_commit(d.sessions[str(a["key"])])
        await d.drain()
        types = [r["type"] for r in await srv.rows("SELECT type FROM crew_events WHERE crew_id = ? ORDER BY seq", (crew_id,))]
        assert "activity.commit" in types[3:], types
        from remembra.relay.crew import outbox as O

        assert not O.read_entries(layout.outbox, ("event",))

        # an ended session's token is dead here too
        await d.end(peer(agents[0]), {"key": a["key"], "reason": "logout"}, grace=False)
        async with httpx.AsyncClient(transport=srv.transport(), base_url="http://test") as http:
            r = await http.post(url, json={"events": [{**commit, "id": "evt-late"}]}, headers={**key, HEADER: token})
            assert r.status_code == 401, r.text


async def test_stall_reports_session_commits_and_adopt_resolves_the_baton_items(tmp_path, agents):
    repo = make_repo(tmp_path / "repo")
    wt_a = add_worktree(repo, tmp_path / "wt-a", "a")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c")
    layout = Layout(tmp_path / "home")
    pid_a, pid_c = agents
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={pid_a, pid_c})
        a = await d.join(peer(pid_a), _join("sess-a", wt_a, pid_a))
        crew_id = str(a["crew_id"])
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        made = await d.api_op(
            peer(pid_a),
            {"key": a["key"], "method": "POST", "path": f"/crews/{crew_id}/tasks",
             "json": {"title": "POS split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []}},
        )  # fmt: skip
        assert made["ok"], made
        task = made["body"]["task"]
        started = await d.api_op(
            peer(pid_a), {"key": a["key"], "method": "POST", "path": f"/tasks/{task['id']}/start", "json": {}}
        )
        assert started["ok"], started
        (wt_a / "src/app/pos/split.ts").write_text("export const split = 2;\n")
        git(wt_a, "commit", "-qam", "pos: split")
        sha = git(wt_a, "rev-parse", "HEAD")
        await d.checkpoint(d.sessions[str(a["key"])], "commit", force=True)  # the commit is now behind the last checkpoint
        (wt_a / "src/app/pos/tender.ts").write_text("export const tender = 2;\n")

        stalled = await d.stall(
            peer(pid_a), {"key": a["key"], "error": "billing_error", "last_assistant_message": "Credit balance is too low"}
        )
        assert stalled["ok"] and stalled["kind"] == "quota" and stalled["baton_ref"], stalled
        [report] = await srv.rows(
            "SELECT commits, baton_ref FROM crew_reports WHERE task_id = ? AND kind = 'stalled'", (task["id"],)
        )
        assert sha in json.loads(report["commits"]), report  # the session's work, not only commits since the last checkpoint
        open_items = await srv.rows(
            "SELECT kind FROM crew_inbox_items WHERE crew_id = ? AND ref_id = ? AND state IN ('open','seen','claimed')",
            (crew_id, task["id"]),
        )
        assert {"baton_available", "baton_reserved"} <= {r["kind"] for r in open_items}, open_items

        c = await d.join(peer(pid_c), _join("sess-c", wt_c, pid_c))  # the join records the baton offer (D33)
        adopted = await d.adopt(peer(pid_c), {"key": c["key"], "task": "T-1"})
        assert adopted["ok"] and adopted["restore"]["restored"], adopted
        assert (wt_c / "src/app/pos/tender.ts").read_text() == "export const tender = 2;\n"
        still_open = await srv.rows(
            "SELECT kind FROM crew_inbox_items WHERE crew_id = ? AND ref_id = ? AND kind LIKE 'baton%'"
            " AND state IN ('open','seen','claimed')",
            (crew_id, task["id"]),
        )
        assert still_open == [], still_open
        types = [r["type"] for r in await srv.rows("SELECT type FROM crew_events WHERE crew_id = ? ORDER BY seq", (crew_id,))]
        assert types.count("inbox.item_resolved") >= 2 and "baton.passed" in types, types
        assert S.DATA_OPEN  # contract import kept honest (the brief block is covered by the model-free E2E)
        assert B.is_clean(str(wt_a)) is False  # A's own checkout is untouched by the adopt
        assert os.path.exists(wt_a / "src/app/pos/tender.ts")
