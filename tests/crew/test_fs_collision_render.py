"""F13: crew_checkpoint stops replaying collisions nobody can act on (fault survey 2026-11-10).

The reply listed every open row the seat was a party to and told the agent "Do not modify these
files further" for files it held under its own claims, against sessions that had ended days
ago. Two servers are exercised: the fixed one, which closes such rows itself, and one that
never closes a row (production 0.17.5, simulated by switching the server's drain rule off), so
the fixed client also behaves against the old server.

Every test goes through ``FastMCP.call_tool`` into the real bridge and a live API server with a
real ``crew.db`` (``tests/crew/mcp_support.py``), like ``tests/crew/test_mcp_crew.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.crew import collisions as CO
from remembra.crew.store import now_iso
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewBridge
from tests.crew.mcp_support import LiveServer, live_server
from tests.crew.test_mcp_crew import (
    Clock,
    _two_agents,
    agent,
    assert_clean,
    call,
    callsign_in,
    crew_id_of,
    session_row,
    use,
)

OWNER_EMAIL = "owner@example.com"
NO_OVERLAP = "COLLISION CHECK: no overlap with other sessions."
STOP = "Do not modify these files further"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-collision-render")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=c))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield c
    server._client = None


@pytest.fixture()
def old_server(monkeypatch):
    """A server that never closes a collision by itself (0.17.5): every live row is "still overlapping"."""

    async def always(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(CO, "_still_overlapping", always)


def _sql(live: LiveServer, sql: str, params: tuple[Any, ...] = ()) -> None:
    db = live.app.state.crew_db

    async def go() -> None:
        async with db.transaction():
            await db.conn.execute(sql, params)

    live.run(go())


def _end(live: LiveServer, project: str, callsign: str, *, days: float = 2) -> None:
    sid = session_row(live, project, callsign)["id"]
    ended = now_iso(datetime.now(UTC) - timedelta(days=days))
    _sql(live, "UPDATE crew_sessions SET state = 'ended', ended_at = ?, end_reason = 'closed' WHERE id = ?", (ended, sid))


def _open_rows(live: LiveServer, project: str) -> list[dict[str, Any]]:
    return live.crew_rows(
        "SELECT kind, subject FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged') ORDER BY id",
        (crew_id_of(live, project),),
    )


def _overlap(a: Any, b: Any, *paths: str) -> str:
    use(a)
    call("crew_checkpoint", files_changed=list(paths), summary="first")
    use(b)
    return call("crew_checkpoint", files_changed=list(paths), summary="second")


def test_a_live_overlap_is_still_listed_with_the_corrective_line(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "liveov")
    out = _overlap(a, b, "src/lib/money.ts")
    assert "COLLISIONS (1):" in out and f"same_file [medium] on src/lib/money.ts with {a_sign}" in out, out
    assert f"{STOP}. Tell @{a_sign} or @mani with crew_say." in out
    assert "older" not in out
    assert_clean(out)


def test_old_server_rows_against_an_ended_session_are_one_quiet_line(live, clock, old_server):
    project, a, a_sign, b, b_sign = _two_agents(live, "endedold")
    out = _overlap(a, b, "src/lib/money.ts", "src/lib/tax.ts", "src/lib/fx.ts")
    assert "COLLISIONS (3):" in out and STOP in out
    _end(live, project, a_sign)
    # an empty checkpoint, seconds later (the bridge's cached session list still says a is live)
    out = call("crew_checkpoint", files_changed=[], summary="nothing new")
    assert len(_open_rows(live, project)) == 3  # the old server kept them
    assert STOP not in out and "COLLISIONS (" not in out, out
    assert "COLLISION CHECK: no overlap with live sessions" in out, out
    assert "3 older notices" in out and "nothing to do" in out, out
    assert_clean(out)


def test_fixed_server_drains_them_so_the_reply_is_clean(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "endednew")
    out = _overlap(a, b, "src/lib/money.ts", "src/lib/tax.ts")
    assert "COLLISIONS (2):" in out
    _end(live, project, a_sign)
    out = call("crew_checkpoint", files_changed=[], summary="nothing new")
    assert NO_OVERLAP in out and STOP not in out, out
    assert _open_rows(live, project) == []
    # the unchanged replay stays clean too
    again = call("crew_checkpoint", files_changed=[], summary="nothing new")
    assert "unchanged" in again and NO_OVERLAP in again, again


def test_old_server_live_rows_come_first_and_stale_ones_are_counted(live, clock, old_server, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "mixed")
    stale = [f"src/lib/gone{n}.ts" for n in range(8)]
    _overlap(a, b, *stale)
    _end(live, project, a_sign)
    # a third, live session overlaps one file with b
    third = agent(live, "claude-code", f"{project}-c", project)
    use(third)
    c_sign = callsign_in(call("crew_status", project_id=project))
    call("crew_checkpoint", files_changed=["src/lib/now.ts"], summary="third")
    use(b)
    session_gets: list[str] = []
    real_call = crew_mod.CrewHttp.call

    def spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "GET" and path.endswith("/sessions"):
            session_gets.append(path)
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", spy)
    out = call("crew_checkpoint", files_changed=["src/lib/now.ts"], summary="overlap with c")
    assert "COLLISIONS (1):" in out, out
    assert f"same_file [medium] on src/lib/now.ts with {c_sign}" in out and f"Tell @{c_sign} or @mani" in out, out
    assert "8 older notices" in out and "gone0.ts" not in out, out
    assert out.count(STOP) == 1
    assert len(session_gets) <= 2, session_gets  # one list for all rows, not one refresh per stale row
    assert_clean(out)


def test_old_server_breach_on_a_zone_the_writer_now_holds_is_not_a_stop_order(live, clock, old_server):
    project, a, a_sign, b, b_sign = _two_agents(live, "adopted")
    use(a)
    assert "GRANTED" in call("crew_claim", action="claim", zone="pos")
    use(b)
    out = call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"], summary="wrote in a's zone")
    assert f"exclusive_breach [high] on src/app/pos/cart.ts (zone pos) with {a_sign}" in out and STOP in out, out
    # the zone moves to the writer (a baton adopt keeps the claim id); a is still live
    b_id = session_row(live, project, b_sign)["id"]
    crew = crew_id_of(live, project)
    _sql(live, "UPDATE crew_claims SET holder_session_id = ?, epoch = epoch + 1 WHERE crew_id = ?", (b_id, crew))
    out = call("crew_checkpoint", files_changed=["src/app/pos/tender.ts"], summary="under my own claim")
    assert STOP not in out and "COLLISIONS (" not in out, out
    assert "1 older notice" in out and "zones you now hold" in out, out


def test_old_server_breach_of_a_reserved_baton_is_still_a_stop_order(live, clock, old_server):
    project, a, a_sign, b, b_sign = _two_agents(live, "baton")
    use(a)
    assert "GRANTED" in call("crew_claim", action="claim", zone="pos")
    use(b)
    call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"], summary="wrote in a's zone")
    # a goes silent and its claim is kept as a baton: the zone is still not b's to edit
    a_id = session_row(live, project, a_sign)["id"]
    _sql(live, "UPDATE crew_sessions SET state = 'lost' WHERE id = ?", (a_id,))
    _sql(live, "UPDATE crew_claims SET state = 'reserved' WHERE holder_session_id = ?", (a_id,))
    out = call("crew_checkpoint", files_changed=[], summary="still there")
    assert "COLLISIONS (1):" in out and "exclusive_breach [high] on src/app/pos/cart.ts" in out and STOP in out, out


def test_when_the_session_list_cannot_be_read_every_row_is_shown(live, clock, old_server, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "nolist")
    _overlap(a, b, "src/lib/money.ts")
    _end(live, project, a_sign)
    real_call = crew_mod.CrewHttp.call

    listed: list[str] = []

    def failing(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "GET" and path.endswith("/collisions"):
            listed.append(path)
        elif listed == [path.replace("/sessions", "/collisions")] and method == "GET" and path.endswith("/sessions"):
            listed.append("failed once")  # the lookup the collision check makes; the callsign lookup after it works
            raise crew_mod.CrewApiError(503, "unavailable", "try again")
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", failing)
    out = call("crew_checkpoint", files_changed=[], summary="blind")
    # not knowing who is live is no reason to hide an overlap: the reply is what it was before
    assert "COLLISIONS (1):" in out and "same_file [medium] on src/lib/money.ts" in out and STOP in out, out


def _third_holder(live: LiveServer, project: str) -> tuple[Any, str]:
    third = agent(live, "claude-code", f"{project}-c", project)
    use(third)
    return third, callsign_in(call("crew_status", project_id=project))


def _breach_then_zone_moves_to_a_third_session(live: LiveServer, prefix: str) -> tuple[str, str, str]:
    project, a, a_sign, b, b_sign = _two_agents(live, prefix)
    use(a)
    assert "GRANTED" in call("crew_claim", action="claim", zone="pos")
    use(b)
    out = call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"], summary="wrote in a's zone")
    assert f"exclusive_breach [high] on src/app/pos/cart.ts (zone pos) with {a_sign}" in out, out
    _, c_sign = _third_holder(live, project)
    c_id = session_row(live, project, c_sign)["id"]
    _sql(
        live,
        "UPDATE crew_claims SET holder_session_id = ?, epoch = epoch + 1 WHERE crew_id = ?",
        (c_id, crew_id_of(live, project)),
    )
    use(b)
    return project, a_sign, c_sign


def test_old_server_breach_of_a_zone_that_moved_to_a_third_session_is_still_a_stop_order(live, clock, old_server):
    project, a_sign, c_sign = _breach_then_zone_moves_to_a_third_session(live, "thirdold")
    out = call("crew_checkpoint", files_changed=[], summary="still dirty")
    # the old server keeps the row against a; the zone is c's now, and it is still not the writer's to edit
    assert "COLLISIONS (1):" in out and "older" not in out, out
    assert f"exclusive_breach [high] on src/app/pos/cart.ts (zone pos) with {c_sign}" in out, out
    assert f"{STOP}. Tell @{c_sign} or @mani with crew_say." in out and a_sign not in out.split("COLLISIONS (1):")[1], out
    assert_clean(out)


def test_fixed_server_reopens_the_breach_against_the_third_session(live, clock):
    project, a_sign, c_sign = _breach_then_zone_moves_to_a_third_session(live, "thirdnew")
    out = call("crew_checkpoint", files_changed=[], summary="still dirty")
    assert "COLLISIONS (1):" in out and "older" not in out, out
    assert f"exclusive_breach [high] on src/app/pos/cart.ts (zone pos) with {c_sign}" in out, out
    assert f"{STOP}. Tell @{c_sign} or @mani with crew_say." in out, out
    rows = live.crew_rows(
        "SELECT kind, session_b FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged')",
        (crew_id_of(live, project),),
    )
    assert rows == [{"kind": "exclusive_breach", "session_b": session_row(live, project, c_sign)["id"]}]
