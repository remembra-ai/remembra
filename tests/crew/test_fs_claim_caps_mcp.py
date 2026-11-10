"""F4, MCP side: ``crew_claim`` says which cap refused and who holds what, releases an own
reserved claim, and ``crew_status`` no longer hides claims past the sixth.

Runs the real API over HTTP (``tests/crew/mcp_support``) through ``FastMCP.call_tool``.
"""

from __future__ import annotations

import re

import pytest

import remembra.mcp.server as server
from remembra.crew import claims as C
from remembra.mcp.crew import CrewBridge
from tests.crew.fs_crew_tag import untagged
from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import OWNER_EMAIL, Clock, agent, assert_clean, call, callsign_in, crew_id_of, project_name, use


@pytest.fixture(name="live", scope="module")
def _live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-claim-caps")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture(name="clock")
def _clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=c))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield c
    server._client = None


def _codex(live, project: str, n: int):
    client = agent(live, "codex", f"{project}-s{n}", project)
    use(client)
    return client, callsign_in(call("crew_status", project_id=project))


def _fleet(live, prefix: str):
    """Two codex sessions hold 3 areas each (the per-agent 6); a third holds nothing."""
    project = project_name(prefix)
    c5, sign5 = _codex(live, project, 5)
    got = call("crew_claim", paths=["a/x.ts", "b/x.ts", "c/x.ts"])
    assert got.count("GRANTED path ") == 3, got
    c3, sign3 = _codex(live, project, 3)
    got = call("crew_claim", paths=["d/x.ts", "e/x.ts", "f/x.ts"])
    assert got.count("GRANTED path ") == 3, got
    c4, sign4 = _codex(live, project, 4)
    return project, (c5, sign5), (c3, sign3), (c4, sign4)


def test_a_fleet_member_holding_nothing_is_served_and_the_refusal_names_the_per_agent_cap(live, clock):
    project, (_, sign5), (_, sign3), (c4, sign4) = _fleet(live, "fleet")
    use(c4)
    assert "holding nothing" in call("crew_status")
    got = call("crew_claim", paths=["lib/model.ts", "lib/assets.ts", "app/api/assets/route.ts"])
    lines = got.splitlines()
    assert lines[0].startswith("GRANTED path lib/model.ts (exclusive, epoch 1, clm_"), got
    assert lines[1].startswith("GRANTED path lib/assets.ts (exclusive, epoch 1, clm_"), got
    refused = lines[2]
    assert refused.startswith("REFUSED: path app/api/assets/route.ts: Per-agent claim limit reached"), got
    assert f"{sign5} x3" in refused and f"{sign3} x3" in refused and "you hold 1" in refused
    assert "of 6 exclusive areas" in refused and "max_exclusive_claims_per_agent" in refused
    assert "Release a claim you no longer need first" not in got
    assert "PARTIAL: 2 of 3 granted" in got
    assert_clean(got)


def test_the_per_session_refusal_lists_what_the_session_holds(live, clock):
    project = project_name("percap")
    _codex(live, project, 1)
    got = call("crew_claim", paths=["a/x.ts", "b/x.ts", "c/x.ts", "d/x.ts"])
    assert got.count("GRANTED path ") == 3, got
    refused = next(ln for ln in got.splitlines() if ln.startswith("REFUSED"))
    assert refused.startswith("REFUSED: path d/x.ts: Per-session claim limit reached: you hold 3 of 3 exclusive areas"), got
    assert "path a/x.ts" in refused and "path b/x.ts" in refused and "path c/x.ts" in refused
    assert "max_exclusive_claims_per_session" in refused
    assert_clean(got)


def test_a_server_without_cap_detail_still_gets_a_truthful_refusal(live, clock, monkeypatch):
    """A 0.17.5 server answers ``claim_cap`` with no numbers: the client works them out from the claims list."""
    project, (_, sign5), (_, sign3), (c4, sign4) = _fleet(live, "oldsrv")
    use(c4)
    assert call("crew_claim", paths=["lib/model.ts"]).startswith("GRANTED path lib/model.ts")
    assert call("crew_claim", action="release", paths=["lib/model.ts"]).startswith("RELEASED path lib/model.ts")
    real = C.ClaimOutcome.body

    def old_body(self):  # the 0.17.5 body: fixed message, no cap detail
        out = real(self)
        if self.error == "claim_cap":
            out.pop("cap", None)
            out["message"] = "Claim limit reached: release a claim first."
        return out

    monkeypatch.setattr(C.ClaimOutcome, "body", old_body)
    monkeypatch.setattr(C, "cap_breach", _always_agent_breach(C.cap_breach))
    got = call("crew_claim", paths=["lib/model.ts"])
    assert got.startswith("REFUSED: path lib/model.ts: claim limit reached, and you hold no exclusive claim"), got
    assert "per-agent limit" in got and f"{sign5} x3" in got and f"{sign3} x3" in got
    assert "Release a claim you no longer need first" not in got
    assert_clean(got)


def _always_agent_breach(real):
    """The 0.17.5 rule: the per-agent cap also refuses a session that holds nothing."""

    async def breach(conn, session, settings, adding):
        found = await real(conn, session, settings, adding)
        if found is None:
            mine, agent = await C._cap_rows(conn, session)
            if len(agent) + 1 > int(settings["max_exclusive_claims_per_agent"]):
                return {"cap": "agent", "limit": 6, "message": "x"}
        return found

    return breach


def test_release_reaches_an_own_reserved_claim_and_frees_the_slot(live, clock):
    """cd-1 held 3 active and 1 reserved path: the release answered NOTHING RELEASED and 7 claims were refused."""
    project = project_name("resv")
    _codex(live, project, 1)
    assert call("crew_claim", paths=["public/hardware/studio.js"]).startswith("GRANTED path public/hardware/studio.js")
    kept = call("crew_claim", action="release", paths=["public/hardware/studio.js"], baton=True)
    assert kept.startswith("RELEASED path public/hardware/studio.js") and untagged(kept, project).endswith("→ reserved"), kept
    # baton=True on a claim that is already a baton keeps it (the server would end it)
    again = call("crew_claim", action="release", paths=["public/hardware/studio.js"], baton=True)
    assert untagged(again, project) == (
        "ALREADY RESERVED path public/hardware/studio.js (exclusive, reserved): it is kept as a baton for the next pickup. "
        "Release it with baton=false to free the area."
    ), again
    rows = live.crew_rows(
        "SELECT state FROM crew_claims WHERE crew_id = ? AND path_glob = ?",
        (crew_id_of(live, project), "public/hardware/studio.js"),
    )
    assert rows == [{"state": "reserved"}]
    released = call("crew_claim", action="release", paths=["public/hardware/studio.js"])
    assert untagged(released, project) == "RELEASED path public/hardware/studio.js (exclusive, reserved) → released", released
    rows = live.crew_rows(
        "SELECT state FROM crew_claims WHERE crew_id = ? AND path_glob = ?",
        (crew_id_of(live, project), "public/hardware/studio.js"),
    )
    assert rows == [{"state": "released"}]


def test_nothing_released_says_what_the_session_does_hold(live, clock):
    project = project_name("held")
    _codex(live, project, 1)
    assert call("crew_claim", paths=["app/shop/page.tsx"]).startswith("GRANTED")
    got = call("crew_claim", action="release", paths=["app/other.tsx"])
    assert got.startswith("NOTHING RELEASED: you hold no live claim on that."), got
    held = untagged(got, project)
    assert re.search(r"You hold: path app/shop/page\.tsx \(exclusive, active, clm_[A-Za-z0-9]+\)\.$", held), got
    assert call("crew_claim", action="release", paths=["app/shop/page.tsx"]).startswith("RELEASED path app/shop/page.tsx")
    assert (
        untagged(call("crew_claim", action="release", paths=["app/shop/page.tsx"]), project)
        == "NOTHING RELEASED: you hold no live claim on that."
    )


def test_status_marks_claims_past_the_sixth(live, clock):
    project = project_name("many")
    _codex(live, project, 1)
    got = call("crew_claim", paths=[f"app/portal/f{i}.tsx" for i in range(8)])
    assert got.count("GRANTED path ") == 8, got
    status = call("crew_status")
    assert "path app/portal/f5.tsx (exclusive, active), +2 more" in status, status


def test_the_tool_description_states_the_limits():
    doc = server.mcp._tool_manager._tools["crew_claim"].description
    assert "3 exclusive areas per session" in doc and "one folder count as one area" in doc
