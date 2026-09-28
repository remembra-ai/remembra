"""Live-session seats (§12) at a plan's limit: credits-out sessions and sub-agents do not hold one.

* An agent stopped on its credits (``quota_blocked``) waits with its work reserved; it gives its
  seat up, so the agent that replaces it joins with a seat, is offered the baton and adopts it
  ("credits run out, the next agent carries on" at the Free plan's 3 and any other limit).
* A sub-agent sits on its parent's seat: it never takes one of its own, never pushes a top-level
  session into observe-only, and has a seat exactly when its parent does.
* Batons are offered to top-level sessions only; a sub-agent joining is not offered one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remembra.crew.sessions import get_session, has_seat, seated_session_count
from remembra.crew.tasks import Caller, TaskService
from tests.crew.sessions_support import OWNER, add_claim, add_task, add_zone, host, join_req, make_env


async def _full_crew_with_a_stall(tmp_path: Path, max_live: int):  # noqa: ANN202
    env = await make_env(tmp_path, max_live=max_live)
    h, tok = await host(env)
    joined = []
    for i in range(max_live):
        joined.append(
            await env.svc.join(user_id=OWNER, req=join_req(f"s-{i}", checkout=f"fp-{i}", host_id=h["id"]), host_token=tok)
        )
    crew = joined[0].crew_id
    a = await get_session(env.conn, joined[0].session["id"])
    assert a is not None
    zone = await add_zone(env, crew, "pos")
    task = await add_task(env, crew, 1, owner=a, zones=[zone])
    await add_claim(env, crew, a, zone_id=zone, task_id=task)
    out = await env.svc.stall(
        a, error="billing_error", facts={"uncommitted_files": ["src/pos/x.ts"]}, baton_ref="refs/remembra/baton/T-1/1"
    )
    assert out["state"] == "quota_blocked"
    return env, h, tok, crew, a, task


@pytest.mark.parametrize("max_live", [3, 5])
async def test_a_credits_out_session_gives_its_seat_to_the_agent_that_replaces_it(tmp_path: Path, max_live: int) -> None:
    env, h, tok, crew, a, task = await _full_crew_with_a_stall(tmp_path, max_live)
    try:
        assert await seated_session_count(env.conn, crew, free_sub_agents=2) == max_live - 1
        d = await env.svc.join(
            user_id=OWNER, req=join_req("s-d", agent="cursor", checkout="fp-d", host_id=h["id"]), host_token=tok
        )
        assert d.observe_only is False and d.upgrade_hint is None
        assert len(d.batons_offered) == 1, d.batons_offered
        assert await has_seat(env.conn, crew, d.session["id"], max_live, free_sub_agents=2)

        # and it can take the baton
        tasks = TaskService(env.log)
        res = await tasks.adopt(crew, task, Caller.for_session(await get_session(env.conn, d.session["id"])))
        assert res.task["status"] == "claimed" and res.task["owner_session_id"] == d.session["id"]

        # one more joins over the cap: observe-only, with the plan's hint
        e = await env.svc.join(user_id=OWNER, req=join_req("s-e", checkout="fp-e", host_id=h["id"]), host_token=tok)
        assert e.observe_only is True and e.upgrade_hint
    finally:
        await env.db.close()


async def test_sub_agents_sit_on_their_parents_seat_and_are_not_offered_batons(tmp_path: Path) -> None:
    env, h, tok, crew, a, _task = await _full_crew_with_a_stall(tmp_path, 3)
    try:
        # the crew has 2 seated sessions (s-1, s-2) and one free seat
        parent = await get_session(env.conn, (await env.one("SELECT id FROM crew_sessions WHERE session_id = 's-1'"))["id"])
        assert parent is not None
        req = join_req("s-1:sub", checkout="fp-1", host_id=h["id"])
        req.parent_session_id = parent["id"]
        sub = await env.svc.join(user_id=OWNER, req=req, host_token=tok, session_token=None)
        assert sub.observe_only is False  # the parent's seat
        assert sub.batons_offered == []  # a sub-agent works for its parent: no baton offer
        assert await seated_session_count(env.conn, crew, free_sub_agents=2) == 2  # it takes no seat of its own

        # the free seat is still there for a real replacement
        d = await env.svc.join(user_id=OWNER, req=join_req("s-d", checkout="fp-d", host_id=h["id"]), host_token=tok)
        assert d.observe_only is False and len(d.batons_offered) == 1

        # a sub-agent of an observe-only session has no seat either
        over = await env.svc.join(user_id=OWNER, req=join_req("s-over", checkout="fp-o", host_id=h["id"]), host_token=tok)
        assert over.observe_only is True
        req = join_req("s-over:sub", checkout="fp-o", host_id=h["id"])
        req.parent_session_id = over.session["id"]
        over_sub = await env.svc.join(user_id=OWNER, req=req, host_token=tok)
        assert over_sub.observe_only is True
    finally:
        await env.db.close()
