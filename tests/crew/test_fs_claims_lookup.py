"""Fault survey F1: the MCP bridge finds the claims a session really holds.

``crew_claim(action="release")`` answered NOTHING RELEASED for a claim that crew_status listed
under YOU: a ``reserved`` claim (after a baton release, a close or an idle park), and a path claim
whose folder a zone covered later (a derived folder zone, a zones.yml upload). Handover by path
and adopt by zone slug missed the same claims.

Every test goes through ``FastMCP.call_tool`` into the real bridge and a live API server with a
real ``crew.db`` (``tests/crew/mcp_support.py``), like ``tests/crew/test_mcp_crew.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewBridge
from tests.crew.mcp_support import LiveServer, live_server
from tests.crew.test_mcp_crew import (
    Clock,
    add_zone,
    agent,
    assert_clean,
    call,
    callsign_in,
    crew_id_of,
    project_name,
    use,
)

OWNER_EMAIL = "owner@example.com"
OLD_EMPTY = "NOTHING RELEASED: you hold no live claim on that."


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-claims-lookup")) as s:
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


def _pair(live: LiveServer, prefix: str) -> tuple[str, Memory, str, Memory, str]:
    """Two agents in a crew that has no zone over ``lib/`` or ``art/`` yet."""
    project = project_name(prefix)
    a = agent(live, "claude-desktop", f"{project}-a", project)
    b = agent(live, "codex", f"{project}-b", project)
    use(a)
    a_sign = callsign_in(call("crew_status", project_id=project))
    add_zone(live, project, "pos", "src/app/pos/**")
    use(b)
    b_sign = callsign_in(call("crew_status", project_id=project))
    return project, a, a_sign, b, b_sign


def _claims(live: LiveServer, project: str, where: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return live.crew_rows(
        "SELECT c.path_glob, c.zone_id, c.resource, c.state, s.callsign FROM crew_claims c"
        " LEFT JOIN crew_sessions s ON s.id = c.holder_session_id"
        f" WHERE c.crew_id = ? AND {where} ORDER BY c.created_at, c.id",
        (crew_id_of(live, project), *params),
    )


def _writes(monkeypatch) -> list[tuple[str, str]]:
    """Every non-GET call the bridge sends from here on, as ``(method, path)``."""
    seen: list[tuple[str, str]] = []
    real_call = crew_mod.CrewHttp.call

    def call_spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method != "GET":
            seen.append((method, path))
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", call_spy)
    return seen


def _set_claim(live: LiveServer, project: str, glob: str, **cols: Any) -> None:
    """Put a claim row into a state only a human action or the reaper produces (fixture shortcut)."""
    db = live.app.state.crew_db
    sets = ", ".join(f"{k} = ?" for k in cols)
    crew = crew_id_of(live, project)

    async def go() -> None:
        async with db.transaction():
            await db.conn.execute(
                f"UPDATE crew_claims SET {sets} WHERE crew_id = ? AND path_glob = ?",
                (*cols.values(), crew, glob),
            )

    live.run(go())


# ---------------------------------------------------------------------------
# Defect 1: the session's own reserved claim
# ---------------------------------------------------------------------------


def test_release_frees_the_sessions_own_reserved_path_claim(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "ownres")
    use(a)
    assert "GRANTED path art/x.psd" in call("crew_claim", paths=["art/x.psd"])
    parked = call("crew_claim", action="release", paths=["art/x.psd"], baton=True)
    assert parked.startswith("RELEASED path art/x.psd") and parked.endswith("→ reserved"), parked
    status = call("crew_status")
    assert "holding path art/x.psd (exclusive, reserved)" in status, status

    released = call("crew_claim", action="release", paths=["art/x.psd"])
    assert released.startswith("RELEASED path art/x.psd (exclusive, reserved)"), released
    assert released.endswith("→ released"), released
    assert _claims(live, project, "c.path_glob = 'art/x.psd'") == [
        {"path_glob": "art/x.psd", "zone_id": None, "resource": None, "state": "released", "callsign": a_sign}
    ]
    assert "holding nothing" in call("crew_status")
    use(b)  # and the other agent is no longer refused
    assert "GRANTED path art/x.psd" in call("crew_claim", paths=["art/x.psd"])


def test_release_frees_the_sessions_own_reserved_zone_claim(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "ownreszone")
    use(a)
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")
    assert call("crew_claim", action="release", zone="pos", baton=True).endswith("→ reserved")
    released = call("crew_claim", action="release", zone="pos")
    assert released.startswith("RELEASED zone pos (exclusive, reserved)") and released.endswith("→ released"), released
    use(b)
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")


def test_release_with_baton_leaves_an_already_reserved_claim_reserved(live, clock, monkeypatch):
    """The server ends a reserved claim on any release; ``baton=True`` must not turn a kept baton into a free area."""
    project, a, a_sign, b, b_sign = _pair(live, "keepbaton")
    use(a)
    call("crew_claim", paths=["art/x.psd"])
    call("crew_claim", action="release", paths=["art/x.psd"], baton=True)
    writes = _writes(monkeypatch)
    again = call("crew_claim", action="release", paths=["art/x.psd"], baton=True)
    assert again.startswith("ALREADY RESERVED path art/x.psd"), again
    assert "baton=false" in again, again
    assert not [w for w in writes if w[1].endswith("/release")], writes
    assert [r["state"] for r in _claims(live, project, "c.path_glob = 'art/x.psd'")] == ["reserved"]


def test_release_leaves_a_human_hold_alone(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _pair(live, "humanhold")
    use(a)
    call("crew_claim", paths=["art/x.psd"])
    _set_claim(live, project, "art/x.psd", state="reserved", reserve_reason="human_hold", reserved_for=None)
    writes = _writes(monkeypatch)
    got = call("crew_claim", action="release", paths=["art/x.psd"])
    assert got.startswith("NOT RELEASED path art/x.psd"), got
    assert "human" in got, got
    assert not [w for w in writes if w[1].endswith("/release")], writes
    assert [r["state"] for r in _claims(live, project, "c.path_glob = 'art/x.psd'")] == ["reserved"]


def test_release_of_a_claim_reserved_for_another_session_reports_the_servers_refusal(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "resother")
    use(a)
    call("crew_claim", paths=["art/x.psd"])
    other = live.crew_rows(
        "SELECT id FROM crew_sessions WHERE crew_id = ? AND callsign = ?", (crew_id_of(live, project), b_sign)
    )[0]["id"]
    _set_claim(live, project, "art/x.psd", state="reserved", reserve_reason="baton", reserved_for=other)
    got = call("crew_claim", action="release", paths=["art/x.psd"])
    assert "RELEASED path" not in got.replace("NOT RELEASED", ""), got
    assert "claim_not_live" in got, got
    assert [r["state"] for r in _claims(live, project, "c.path_glob = 'art/x.psd'")] == ["reserved"]


# ---------------------------------------------------------------------------
# Defect 2: a path claim that a zone covered later
# ---------------------------------------------------------------------------


def _orphan(live: LiveServer, prefix: str) -> tuple[str, Memory, str, Memory, str]:
    """``a`` holds a path claim on ``lib/sales.ts``; then a zone ``lib`` arrives over it (as a derived
    folder zone or a zones.yml upload does). The claim stays path-only: ``zone_id`` NULL."""
    project, a, a_sign, b, b_sign = _pair(live, prefix)
    use(a)
    assert "GRANTED path lib/sales.ts" in call("crew_claim", paths=["lib/sales.ts"])
    add_zone(live, project, "lib", "lib/**")
    assert _claims(live, project, "c.path_glob = 'lib/sales.ts'") == [
        {"path_glob": "lib/sales.ts", "zone_id": None, "resource": None, "state": "active", "callsign": a_sign}
    ]
    use(b)  # the orphan blocks the other agent's zone claim
    assert call("crew_claim", zone="lib").startswith(f"REFUSED: zone lib held EXCLUSIVELY by {a_sign}")
    use(a)
    return project, a, a_sign, b, b_sign


def test_release_by_path_frees_a_path_claim_a_zone_covered_later(live, clock):
    project, a, a_sign, b, b_sign = _orphan(live, "orphanpath")
    assert "holding path lib/sales.ts (exclusive, active)" in call("crew_status")
    released = call("crew_claim", action="release", paths=["lib/sales.ts"])
    assert released == "RELEASED path lib/sales.ts (exclusive, active) → released", released
    assert [r["state"] for r in _claims(live, project, "c.path_glob = 'lib/sales.ts'")] == ["released"]
    use(b)
    assert call("crew_claim", zone="lib").startswith("GRANTED zone lib")


def test_release_by_zone_slug_frees_a_path_claim_inside_that_zone(live, clock):
    project, a, a_sign, b, b_sign = _orphan(live, "orphanzone")
    released = call("crew_claim", action="release", zone="lib")
    assert released == "RELEASED path lib/sales.ts (exclusive, active) → released", released
    use(b)
    assert call("crew_claim", zone="lib").startswith("GRANTED zone lib")


def test_release_by_folder_frees_the_path_claims_under_it(live, clock):
    project, a, a_sign, b, b_sign = _orphan(live, "orphandir")
    released = call("crew_claim", action="release", paths=["lib"])
    assert released == "RELEASED path lib/sales.ts (exclusive, active) → released", released


def test_adopt_then_release_by_path_under_a_later_zone(live, clock):
    """The 2026-11-09 sequence: ADOPTED lib/…, then NOTHING RELEASED, then still held."""
    project, a, a_sign, b, b_sign = _orphan(live, "adoptrel")
    _set_claim(live, project, "lib/sales.ts", state="reserved", reserve_reason="baton", reserved_for=None)
    adopted = call("crew_claim", action="adopt", paths=["lib/sales.ts"])
    assert adopted.startswith("ADOPTED lib/sales.ts: you now hold path lib/sales.ts (exclusive, active)"), adopted
    released = call("crew_claim", action="release", paths=["lib/sales.ts"])
    assert released == "RELEASED path lib/sales.ts (exclusive, active) → released", released
    assert "holding nothing" in call("crew_status")


def test_adopt_by_zone_slug_takes_a_reserved_path_claim_inside_that_zone(live, clock):
    project, a, a_sign, b, b_sign = _orphan(live, "adoptzone")
    _set_claim(live, project, "lib/sales.ts", state="reserved", reserve_reason="baton", reserved_for=None)
    adopted = call("crew_claim", action="adopt", zone="lib")
    assert adopted.startswith("ADOPTED zone lib: you now hold path lib/sales.ts (exclusive, active)"), adopted


def test_handover_by_path_offers_a_path_claim_a_zone_covered_later(live, clock):
    project, a, a_sign, b, b_sign = _orphan(live, "orphanhand")
    offered = call("crew_claim", action="handover", paths=["lib/sales.ts"], to=f"@{b_sign}")
    assert offered.startswith(f"OFFERED path lib/sales.ts (exclusive, active) to {b_sign}"), offered


def test_a_zone_claim_is_not_released_by_a_path_outside_it(live, clock):
    """The wider match must not free more than was named."""
    project, a, a_sign, b, b_sign = _pair(live, "narrow")
    use(a)
    call("crew_claim", zone="pos")
    call("crew_claim", paths=["art/x.psd", "docs/notes.md"])
    got = call("crew_claim", action="release", paths=["art/x.psd"])
    assert got == "RELEASED path art/x.psd (exclusive, active) → released", got
    status = call("crew_status")
    assert "zone pos (exclusive, active)" in status and "path docs/notes.md (exclusive, active)" in status, status
    got = call("crew_claim", action="release", zone="pos")
    assert got == "RELEASED zone pos (exclusive, active) → released", got
    assert "path docs/notes.md (exclusive, active)" in call("crew_status")


# ---------------------------------------------------------------------------
# Defect 3: the empty answer says what the session holds and who holds the target
# ---------------------------------------------------------------------------


def test_the_empty_answer_names_what_the_session_holds(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "empty")
    use(a)
    call("crew_claim", paths=["art/x.psd"])
    got = call("crew_claim", action="release", paths=["docs/other.md"])
    assert got.startswith(OLD_EMPTY), got
    assert re.search(r"You hold: path art/x\.psd \(exclusive, active, clm_[A-Za-z0-9]+\)", got), got
    assert_clean(got)
    handed = call("crew_claim", action="handover", paths=["docs/other.md"], to=f"@{b_sign}")
    assert handed.startswith("NOT HANDED OVER: you hold no active claim on that."), handed
    assert "You hold: path art/x.psd (exclusive, active, clm_" in handed, handed


def test_the_empty_answer_names_the_other_holder(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "emptyother")
    use(a)
    call("crew_claim", zone="pos")
    use(b)
    got = call("crew_claim", action="release", zone="pos")
    assert got.startswith(OLD_EMPTY), got
    assert f"zone pos (exclusive, active) is held by {a_sign}" in got, got
    assert "You hold nothing" in got, got


def test_the_empty_answer_explains_a_prefixed_target(live, clock):
    """Agents sent ``paths=["zone:hardware"]`` and ``zone="path:app/…"`` and got the same fixed string."""
    project, a, a_sign, b, b_sign = _pair(live, "prefixed")
    use(a)
    call("crew_claim", zone="pos")
    got = call("crew_claim", action="release", paths=["zone:pos"])
    assert got.startswith(OLD_EMPTY), got
    assert 'zone="pos"' in got, got
    got = call("crew_claim", action="release", zone="path:art/x.psd")
    assert got.startswith(OLD_EMPTY), got
    assert 'paths=["art/x.psd"]' in got, got
    got = call("crew_claim", action="release", zone="zone:pos")
    assert 'zone="pos"' in got, got
    assert "zone pos (exclusive, active)" in call("crew_status")  # nothing was guessed and released


def test_a_session_that_holds_nothing_gets_the_plain_answer(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "plain")
    use(a)
    assert call("crew_claim", action="release", zone="pos") == OLD_EMPTY


def test_a_zone_added_after_the_zone_list_was_read_is_found(live, clock):
    """Zones are cached per seat for a few seconds; a zone that arrived since (a derived folder zone)
    must not answer "Unknown zone" for the claim or release that names it."""
    project, a, a_sign, b, b_sign = _pair(live, "latezone")
    use(a)
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")  # reads and caches the zone list
    add_zone(live, project, "late", "late/**")
    assert call("crew_claim", zone="late").startswith("GRANTED zone late")
    assert call("crew_claim", action="release", zone="late") == "RELEASED zone late (exclusive, active) → released"
    assert call("crew_claim", zone="nosuchzone").startswith("CREW: Unknown zone")
