"""A human's "Hand baton to…" closes every item about that baton and moves the lanes' current task.

The stall path raises ``baton_available`` (Needs you) and ``baton_reserved`` (crew) items; assign
used to resolve only ``task_ready`` and ``baton``, so the owner kept a stale "Baton T-1 is waiting
for pickup" item after handing it over. The ``task.assigned`` event it emits, run through the
reducer the dashboard mirrors, gives the new owner the task as its current one and clears the old.
"""

from __future__ import annotations

from pathlib import Path

from remembra.crew import reducer
from remembra.crew.core import CrewCore
from remembra.crew.events import fetch_events
from remembra.crew.sessions import get_session
from remembra.crew.tasks import Caller, TaskService
from tests.crew.sessions_support import OWNER, add_claim, add_task, add_zone, host, join_req, make_env


async def test_assign_after_a_stall_closes_the_baton_items_and_moves_the_lane(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        h, tok = await host(env)
        a_join = await env.svc.join(user_id=OWNER, req=join_req("s-a", checkout="fp-a", host_id=h["id"]), host_token=tok)
        crew = a_join.crew_id
        a = await get_session(env.conn, a_join.session["id"])
        assert a is not None
        zone = await add_zone(env, crew, "pos")
        task = await add_task(env, crew, 1, owner=a, zones=[zone])
        await add_claim(env, crew, a, zone_id=zone, task_id=task)
        await env.svc.stall(a, error="billing_error", facts={}, baton_ref="refs/remembra/baton/T-1/1")
        b_join = await env.svc.join(
            user_id=OWNER, req=join_req("s-b", agent="cursor", checkout="fp-b", host_id=h["id"]), host_token=tok
        )
        open_before = await env.all(
            "SELECT dedupe_key FROM crew_inbox_items WHERE crew_id = ? AND state NOT IN ('resolved','dismissed')", (crew,)
        )
        assert {r["dedupe_key"] for r in open_before} & {f"baton_available:{task}", f"baton_reserved:{task}"}, open_before

        snapshot = await CrewCore(env.db).snapshot(crew)
        before_seq = int(snapshot["as_of_seq"])
        res = await TaskService(env.log).assign(crew, task, Caller.for_human(OWNER), b_join.session["id"])
        assert res.task["owner_session_id"] == b_join.session["id"]

        still_open = await env.all(
            "SELECT dedupe_key FROM crew_inbox_items WHERE crew_id = ? AND state NOT IN ('resolved','dismissed')"
            " AND dedupe_key LIKE 'baton%'",
            (crew,),
        )
        assert still_open == [], still_open

        events = await fetch_events(env.conn, crew, after_seq=before_seq)
        assert "task.assigned" in [e["type"] for e in events]
        state = reducer.reduce(snapshot, [{"type": "crew.event", "crew_id": crew, "data": e} for e in events])
        assert state["sessions"][b_join.session["id"]]["current_task_id"] == task
        assert state["sessions"][a["id"]]["current_task_id"] is None
        # the lane agrees with the server's own session rows
        rows = {r["id"]: r["current_task_id"] for r in await env.all("SELECT id, current_task_id FROM crew_sessions")}
        assert rows[b_join.session["id"]] == task and rows[a["id"]] is None
    finally:
        await env.db.close()
