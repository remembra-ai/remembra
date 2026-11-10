"""F10, MCP side: ``crew_claim(paths=[...])`` inside a TEMPORARY folder zone claims the files, not the folder.

Runs the real API over HTTP (``tests/crew/mcp_support``) through ``FastMCP.call_tool``.
"""

from __future__ import annotations

import pytest

import remembra.mcp.server as server
from remembra.crew import claims as C
from remembra.mcp.crew import CrewBridge
from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import (
    OWNER_EMAIL,
    Clock,
    add_zone,
    agent,
    assert_clean,
    call,
    callsign_in,
    crew_id_of,
    owner_headers,
    project_name,
    use,
)

TREE = {
    "name": ".",
    "files": 2,
    "children": [
        {"name": "lib", "files": 9, "children": []},
        {
            "name": "app",
            "files": 1,
            "children": [
                {"name": "api", "files": 6, "children": []},
                {"name": "portal", "files": 4, "children": []},
            ],
        },
    ],
}


@pytest.fixture(name="live", scope="module")
def _live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-temp-zones")) as s:
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


def _bootstrap(live, prefix: str):
    """Claude and Codex in a repo with no zones.yml: the tree upload creates the temporary folder zones."""
    project = project_name(prefix)
    a = agent(live, "claude-desktop", f"{project}-a", project)
    b = agent(live, "codex", f"{project}-b", project)
    use(a)
    a_sign = callsign_in(call("crew_status", project_id=project))
    use(b)
    b_sign = callsign_in(call("crew_status", project_id=project))
    res = live.api("PUT", f"/crews/{crew_id_of(live, project)}/tree", headers=owner_headers(live), json={"tree": TREE})
    assert len(res["bootstrap_zone_ids"]) == 3, res
    zones = live.crew_rows("SELECT slug, source FROM crew_zones WHERE crew_id = ? AND builtin = 0", (crew_id_of(live, project),))
    assert sorted((z["slug"], z["source"]) for z in zones) == [
        ("api", "suggested"),
        ("lib", "suggested"),
        ("portal", "suggested"),
    ]
    return project, a, a_sign, b, b_sign


def _claims(live, project: str) -> list[tuple[str | None, str | None, str]]:
    rows = live.crew_rows(
        "SELECT c.path_glob, z.slug, s.callsign FROM crew_claims c JOIN crew_sessions s ON s.id = c.holder_session_id"
        " LEFT JOIN crew_zones z ON z.id = c.zone_id WHERE c.crew_id = ? AND c.state = 'active' ORDER BY c.created_at",
        (crew_id_of(live, project),),
    )
    return [(r["path_glob"], r["slug"], r["callsign"]) for r in rows]


def test_two_agents_claim_different_files_in_the_same_temporary_zone(live, clock):
    """Claude asked for lib/team-access.ts and was refused 'zone lib held EXCLUSIVELY by codex-5',
    who held one other file there."""
    project, a, a_sign, b, b_sign = _bootstrap(live, "lib")
    use(b)
    got = call("crew_claim", paths=["lib/sales-research.ts"])
    assert got.startswith("GRANTED path lib/sales-research.ts (exclusive, epoch 1, clm_"), got
    use(a)
    got = call("crew_claim", paths=["lib/team-access.ts", "app/portal/portal-client.tsx", "app/portal/page.tsx"])
    lines = got.splitlines()
    assert lines[0].startswith("GRANTED path lib/team-access.ts (exclusive, epoch 1, clm_"), got
    assert lines[1].startswith("GRANTED path app/portal/portal-client.tsx (") and lines[2].startswith(
        "GRANTED path app/portal/page.tsx ("
    ), got
    assert "GRANTED zone" not in got
    assert _claims(live, project) == [
        ("lib/sales-research.ts", None, b_sign),
        ("lib/team-access.ts", None, a_sign),
        ("app/portal/portal-client.tsx", None, a_sign),
        ("app/portal/page.tsx", None, a_sign),
    ]
    assert_clean(got)
    # the same file is refused, naming the file's holder
    use(b)
    same = call("crew_claim", paths=["lib/team-access.ts"])
    assert same.startswith(f"REFUSED: path lib/team-access.ts held EXCLUSIVELY by {a_sign}."), same
    # the whole zone is refused truthfully: the holder has one file in it, not the zone
    whole = call("crew_claim", zone="lib")
    assert whole.startswith(f"REFUSED: zone lib: {a_sign} holds path lib/team-access.ts inside it (exclusive)."), whole
    assert "zone lib held EXCLUSIVELY by" not in whole
    assert f'crew_say(to="@{a_sign}", kind="request_release")' in whole
    assert_clean(whole)
    # release by path reaches the file claim
    assert call("crew_claim", action="release", paths=["lib/sales-research.ts"]).startswith("RELEASED path lib/sales-research.ts")
    status = call("crew_status")
    assert f"{a_sign} holds a path claim" in status or "lib/team-access.ts" in status, status


def test_a_whole_zone_holder_is_named_as_such_to_a_file_claimer(live, clock):
    project, a, a_sign, b, b_sign = _bootstrap(live, "api")
    use(a)
    assert call("crew_claim", zone="api").startswith("GRANTED zone api (exclusive, epoch 1, clm_")  # an explicit zone claim
    use(b)
    refused = call("crew_claim", paths=["app/api/fulfil/route.ts"])
    assert refused.startswith(f"REFUSED: path app/api/fulfil/route.ts: the whole zone api is held EXCLUSIVELY by {a_sign}."), (
        refused
    )
    assert f'crew_say(to="@{a_sign}", kind="request_release")' in refused
    queued = call("crew_claim", paths=["app/api/fulfil/route.ts"], wait_s=1)
    assert queued.startswith("QUEUED path app/api/fulfil/route.ts") and f"exclusive by {a_sign} (zone api)" in queued, queued


def test_a_server_that_refuses_files_inside_zones_gets_the_zone_claim_and_says_so(live, clock, monkeypatch):
    """A 0.17.5 server answers 422 zone_path: the client claims the zone once, as before, and names what it locked."""
    project, a, a_sign, b, b_sign = _bootstrap(live, "old")
    monkeypatch.setattr(C, "temporary_file_zone", lambda zone: False)
    use(a)
    got = call("crew_claim", paths=["lib/a.ts", "lib/b.ts", "app/portal/page.tsx"])
    lines = got.splitlines()
    assert lines[0].startswith("GRANTED zone lib (exclusive, epoch 1, clm_"), got
    assert lines[1].startswith("GRANTED zone portal (exclusive, epoch 1, clm_"), got
    assert got.count("GRANTED") == 2
    assert "- lib/a.ts, lib/b.ts: this server claims whole zones only: all of zone lib is now yours" in got
    assert _claims(live, project) == [(None, "lib", a_sign), (None, "portal", a_sign)]
    assert_clean(got)


def test_a_declared_zone_is_still_claimed_whole_and_the_answer_says_so(live, clock):
    project = project_name("decl")
    a = agent(live, "claude-desktop", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    add_zone(live, project, "billing", "src/app/billing/**")
    got = call("crew_claim", paths=["src/app/billing/invoice.ts"])
    assert got.startswith("GRANTED zone billing (exclusive, epoch 1, clm_"), got
    assert "- src/app/billing/invoice.ts: inside declared zone billing: the whole zone is claimed" in got


def test_a_path_claim_that_a_zone_now_covers_can_still_be_released(live, clock):
    """codex-5 held lib/sales-research.ts from before the zones existed: release answered NOTHING RELEASED
    and it blocked the whole zone until a human stepped in."""
    project = project_name("stuck")
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    assert call("crew_claim", paths=["tools/sales-research.ts"]).startswith("GRANTED path tools/sales-research.ts")
    add_zone(live, project, "tools", "tools/**")  # the zone arrives after the claim
    status = call("crew_status")
    assert "holding path tools/sales-research.ts (exclusive, active)" in status, status
    released = call("crew_claim", action="release", paths=["tools/sales-research.ts"])
    assert released == "RELEASED path tools/sales-research.ts (exclusive, active) → released", released
    assert _claims(live, project) == []
