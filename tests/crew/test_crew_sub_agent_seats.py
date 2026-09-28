"""Sub-agents and the plan's live-session seats (owner decision 13, security finding SEC-3).

Each top-level session runs its first 2 live sub-agents on its own seat. Every further live
sub-agent counts toward the plan's ``max_crew_sessions_live`` like a session of its own: it takes a
free seat, or it joins observe-only (never refused) and cannot claim, the same 403 ``observe_only``
with the plan's upgrade hint that a top-level session over the limit gets (562b79a). Sub-agents that
a sub-agent starts count toward its top-level session's 2, so nesting opens no extra room. Seats go in
join order, and an observe-only sub-agent gets a seat as soon as an earlier one ends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from remembra.cloud.plans import PLANS
from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.limits import CrewLimits, crew_limits_for_tier
from remembra.crew.sessions import JoinResult, get_session, has_seat, seat_queue, seated_session_count
from remembra.crew.tasks import Caller, CrewServiceError, TaskService
from tests.crew.sessions_support import OWNER, Env, add_task, host, join_req, make_env

FREE = crew_limits_for_tier("free")  # 3 live sessions per crew, 2 sub-agents per session on its seat
HINT = "Solo allows 5 live sessions per crew."


async def _free_limits(_owner: str) -> CrewLimits:
    return FREE


@pytest.fixture
async def env(tmp_path: Path):
    env = await make_env(tmp_path)
    env.svc.limits_for = _free_limits
    h, tok = await host(env)
    env.h, env.tok = h, tok  # type: ignore[attr-defined]
    try:
        yield env
    finally:
        await env.db.close()


async def _top(env: Env, name: str) -> JoinResult:
    env.clock.advance(1)  # seats go in join order
    return await env.svc.join(
        user_id=OWNER,
        req=join_req(name, checkout=f"fp-{name}", host_id=env.h["id"]),  # type: ignore[attr-defined]
        host_token=env.tok,  # type: ignore[attr-defined]
    )


async def _sub(env: Env, parent: JoinResult, name: str) -> JoinResult:
    env.clock.advance(1)
    req = join_req(name, checkout=parent.session["checkout_fp"], host_id=env.h["id"])  # type: ignore[attr-defined]
    req.parent_session_id = parent.session["id"]
    # the same verified agent key as the parent proves the link
    return await env.svc.join(user_id=OWNER, req=req, host_token=env.tok)  # type: ignore[attr-defined]


async def _seated(env: Env, joined: JoinResult) -> bool:
    return await has_seat(
        env.conn, joined.crew_id, joined.session["id"], FREE.max_sessions_live, free_sub_agents=FREE.free_sub_agents_per_parent
    )


async def _holders(env: Env, crew_id: str) -> int:
    return await seated_session_count(env.conn, crew_id, free_sub_agents=FREE.free_sub_agents_per_parent)


async def _observe_only_flags(env: Env, crew_id: str) -> dict[str, bool]:
    return {v["callsign"]: v["observe_only"] for v in await env.svc.list_sessions(crew_id, "live")}


def test_every_plan_runs_two_sub_agents_per_session_on_its_seat() -> None:
    for tier, plan in PLANS.items():
        assert plan.crew_free_sub_agents_per_parent == 2, tier
        assert crew_limits_for_tier(tier).free_sub_agents_per_parent == 2, tier
        # a per-crew field: never multiplied by Team seats
        assert plan.scaled(12).crew_free_sub_agents_per_parent == 2, tier


async def test_two_sub_agents_sit_on_their_parents_seat(env: Env) -> None:
    """N: at the allowance a parent's sub-agents take no seat, so the plan's 3 seats stay for 3 sessions."""
    p = await _top(env, "p")
    s1, s2 = await _sub(env, p, "p:s1"), await _sub(env, p, "p:s2")
    assert (s1.observe_only, s2.observe_only) == (False, False) and s1.upgrade_hint is None
    assert await _holders(env, p.crew_id) == 1
    q, r = await _top(env, "q"), await _top(env, "r")
    assert (q.observe_only, r.observe_only) == (False, False)
    assert await _holders(env, p.crew_id) == 3
    over = await _top(env, "over")
    assert over.observe_only is True and over.upgrade_hint == HINT
    for joined in (p, s1, s2, q, r):
        assert await _seated(env, joined), joined.session["callsign"]


async def test_the_third_sub_agent_takes_a_seat_and_the_one_past_the_limit_is_observe_only(env: Env) -> None:
    """N+1: past its 2 a sub-agent counts; over the plan's limit it joins observe-only with the upgrade hint."""
    p, q = await _top(env, "p"), await _top(env, "q")
    s1, s2 = await _sub(env, p, "p:s1"), await _sub(env, p, "p:s2")
    s3 = await _sub(env, p, "p:s3")
    assert s3.observe_only is False  # the crew's third seat
    assert await _holders(env, p.crew_id) == 3
    s4 = await _sub(env, p, "p:s4")
    assert s4.observe_only is True and s4.upgrade_hint == HINT
    assert s4.session_token  # joined, not refused: it still gets DO NOT TOUCH and the brief
    events = await env.events(p.crew_id, types=("session.joined",))
    assert [e["payload"]["observe_only"] for e in events if e["payload"]["session"]["id"] == s4.session["id"]] == [True]
    # the seats are full, so a new top-level session is observe-only too
    r = await _top(env, "r")
    assert r.observe_only is True and r.upgrade_hint == HINT
    flags = await _observe_only_flags(env, p.crew_id)
    names = {x.session["id"]: x.session["callsign"] for x in (p, q, s1, s2, s3, s4, r)}
    assert flags == {
        names[p.session["id"]]: False,
        names[q.session["id"]]: False,
        names[s1.session["id"]]: False,
        names[s2.session["id"]]: False,
        names[s3.session["id"]]: False,
        names[s4.session["id"]]: True,
        names[r.session["id"]]: True,
    }


async def test_each_parent_has_its_own_two(env: Env) -> None:
    """Two parents: each runs 2 sub-agents on its own seat; one parent's third counts, the other's first two do not."""
    p, q = await _top(env, "p"), await _top(env, "q")
    subs = [await _sub(env, p, "p:s1"), await _sub(env, q, "q:s1"), await _sub(env, p, "p:s2"), await _sub(env, q, "q:s2")]
    assert [s.observe_only for s in subs] == [False] * 4
    assert await _holders(env, p.crew_id) == 2
    r = await _top(env, "r")
    assert r.observe_only is False  # the third seat was still free
    q3 = await _sub(env, q, "q:s3")
    assert q3.observe_only is True and q3.upgrade_hint == HINT
    assert all([await _seated(env, s) for s in subs])


async def test_sub_agents_of_a_sub_agent_count_toward_the_top_level_sessions_two(env: Env) -> None:
    p = await _top(env, "p")
    s1 = await _sub(env, p, "p:s1")
    s1a = await _sub(env, s1, "p:s1:a")
    assert s1a.observe_only is False and await _holders(env, p.crew_id) == 1
    s1b = await _sub(env, s1, "p:s1:b")  # p's third sub-agent, one level down
    assert s1b.observe_only is False and await _holders(env, p.crew_id) == 2
    q = await _top(env, "q")
    assert q.observe_only is False and await _holders(env, p.crew_id) == 3
    s1c = await _sub(env, s1, "p:s1:c")
    assert s1c.observe_only is True and s1c.upgrade_hint == HINT


async def test_a_sub_agent_of_an_observe_only_session_has_no_seat_within_its_two(env: Env) -> None:
    await _top(env, "p")
    await _top(env, "q")
    await _top(env, "r")
    over = await _top(env, "over")
    sub = await _sub(env, over, "over:s1")
    assert over.observe_only is True and sub.observe_only is True and sub.upgrade_hint == HINT
    assert await _holders(env, over.crew_id) == 4  # the four top-level sessions; the sub-agent is within over's two


async def _principal(env: Env, joined: JoinResult) -> Z.Principal:
    row = await get_session(env.conn, joined.session["id"])
    assert row is not None
    return Z.Principal.for_session(row)


async def _caller(env: Env, joined: JoinResult) -> Caller:
    row = await get_session(env.conn, joined.session["id"])
    assert row is not None
    return Caller.for_session(row)


def _refused_observe_only(e: pytest.ExceptionInfo[Any]) -> None:
    assert e.value.status == 403 and e.value.error == "observe_only", str(e.value)
    assert e.value.extra["upgrade_hint"] == HINT


async def test_an_over_cap_sub_agent_cannot_claim_until_an_earlier_sub_agent_ends(env: Env) -> None:
    """The observe-only rule of 562b79a holds for a sub-agent over the limit: claims and tasks answer 403."""
    p, q, r = await _top(env, "p"), await _top(env, "q"), await _top(env, "r")
    s1, s2 = await _sub(env, p, "p:s1"), await _sub(env, p, "p:s2")
    s3 = await _sub(env, p, "p:s3")
    assert s3.observe_only is True
    crew = p.crew_id
    ops = Z.CrewOps(env.log, None, FREE)
    tasks = TaskService(env.log, limits_for=_free_limits)
    task = await add_task(env, crew, 1, owner=None, status="ready")

    with pytest.raises(Z.CrewOpError) as e:
        await C.request_claim(ops, crew, await _principal(env, s3), resource="deploy:vercel")
    _refused_observe_only(e)
    with pytest.raises(CrewServiceError) as e2:
        await tasks.claim(crew, task, await _caller(env, s3))
    _refused_observe_only(e2)
    assert await env.all("SELECT id FROM crew_claims WHERE holder_session_id = ?", (s3.session["id"],)) == []

    # the two within the allowance claim as before
    held = await C.request_claim(ops, crew, await _principal(env, s1), resource="schema:main")
    assert held.status == "granted" and held.claim is not None

    # s2 ends: s3 is now one of p's two and sits on p's seat
    row = await get_session(env.conn, s2.session["id"])
    assert row is not None
    await env.svc.leave(row, reason="other", facts={}, summary=None, baton=False, baton_ref=None)
    assert await _seated(env, s3)
    assert (await _observe_only_flags(env, crew))[s3.session["callsign"]] is False
    res = await tasks.claim(crew, task, await _caller(env, s3))
    assert res.task["status"] == "claimed" and res.task["owner_session_id"] == s3.session["id"]
    assert (q.observe_only, r.observe_only) == (False, False)


def _row(sid: str, second: int, parent: str | None = None, state: str = "active") -> dict[str, Any]:
    return {"id": sid, "joined_at": f"2026-09-25T20:00:{second:02d}Z", "parent_session_id": parent, "state": state}


def test_the_seat_queue_counts_only_sub_agents_past_the_two_and_needs_a_seated_top_level_session() -> None:
    rows = [
        _row("cs_p", 1),
        _row("cs_s1", 2, "cs_p", state="quota_blocked"),  # stopped on its credits: uses none of p's two
        _row("cs_s2", 3, "cs_p"),
        _row("cs_s3", 4, "cs_p"),
        _row("cs_s4", 5, "cs_p"),  # p's third live sub-agent: counted
        _row("cs_orphan", 6, "cs_gone"),  # its parent is not live: no seat, not counted
    ]
    two = seat_queue(rows, 2, 2)
    assert two.counted == ("cs_p", "cs_s4")
    assert two.seated == {"cs_p", "cs_s1", "cs_s2", "cs_s3", "cs_s4"}
    one = seat_queue(rows, 1, 2)
    assert one.seated == {"cs_p", "cs_s1", "cs_s2", "cs_s3"}  # s4 is behind p in the queue

    # a sub-agent listed before its top-level session (it re-joined later) still needs that session seated
    later = seat_queue([_row("cs_q", 0), _row("cs_s", 1, "cs_p"), _row("cs_p", 2)], 1, 2)
    assert later.counted == ("cs_q", "cs_p") and later.seated == {"cs_q"}
