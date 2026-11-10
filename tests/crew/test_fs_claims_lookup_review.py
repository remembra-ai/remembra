"""Fault survey F1, F24, F40: review findings on the claims lookup fix.

* A zone slug names what is inside the zone: a path claim in a part the zone excludes is not
  released, handed over or adopted with it.
* Naming one file does not release a wider claim that happens to cover it (a folder still does).
* Adopt by zone slug with the zone's own baton and a path baton under it takes the zone's first.
* A 429 that did not come from the crew server as JSON keeps the plain answer.
* The gate's literal-path equality follows the volume's case rule.

The MCP tests go through ``FastMCP.call_tool`` into the real bridge and a live API server with a
real ``crew.db`` (``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewApiError, CrewBridge, error_text
from tests.crew.gatecore_support import find, run, snapshot
from tests.crew.mcp_support import LiveServer, live_server
from tests.crew.test_mcp_crew import (
    Clock,
    add_zone,
    agent,
    call,
    callsign_in,
    crew_id_of,
    owner_headers,
    project_name,
    session_row,
    use,
)

OWNER_EMAIL = "owner@example.com"
OLD_EMPTY = "NOTHING RELEASED: you hold no live claim on that."


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-claims-lookup-review")) as s:
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
    project = project_name(prefix)
    a = agent(live, "claude-desktop", f"{project}-a", project)
    b = agent(live, "codex", f"{project}-b", project)
    use(a)
    a_sign = callsign_in(call("crew_status", project_id=project))
    add_zone(live, project, "pos", "src/app/pos/**")
    use(b)
    b_sign = callsign_in(call("crew_status", project_id=project))
    return project, a, a_sign, b, b_sign


def _states(live: LiveServer, project: str, where: str) -> list[tuple[Any, str, Any]]:
    rows = live.crew_rows(
        "SELECT c.path_glob, c.state, s.callsign FROM crew_claims c"
        " LEFT JOIN crew_sessions s ON s.id = c.holder_session_id"
        f" WHERE c.crew_id = ? AND {where} ORDER BY c.created_at, c.id",
        (crew_id_of(live, project),),
    )
    return [(r["path_glob"], r["state"], r["callsign"]) for r in rows]


def _set_claims(live: LiveServer, project: str, where: str, **cols: Any) -> None:
    """Put claim rows into a state only the reaper, a baton save or a human produces (fixture shortcut)."""
    db = live.app.state.crew_db
    sets = ", ".join(f"{k} = ?" for k in cols)
    crew = crew_id_of(live, project)

    async def go() -> None:
        async with db.transaction():
            await db.conn.execute(f"UPDATE crew_claims SET {sets} WHERE crew_id = ? AND {where}", (*cols.values(), crew))

    live.run(go())


def _writes(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real_call = crew_mod.CrewHttp.call

    def call_spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method != "GET" and not path.endswith("/match"):
            seen.append((method, path))
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", call_spy)
    return seen


def _zone_with_exclude(live: LiveServer, prefix: str) -> tuple[str, Memory, str, Memory, str]:
    """``a`` holds path claims on ``lib/vendor/x.ts`` and ``lib/sales.ts``; then zone ``lib`` arrives with
    ``lib/**`` included and ``lib/vendor/**`` excluded."""
    project, a, a_sign, b, b_sign = _pair(live, prefix)
    use(a)
    assert "GRANTED" in call("crew_claim", paths=["lib/vendor/x.ts"])
    assert "GRANTED" in call("crew_claim", paths=["lib/sales.ts"])
    live.api(
        "POST",
        f"/crews/{crew_id_of(live, project)}/zones",
        headers=owner_headers(live),
        json={"slug": "lib", "include": ["lib/**"], "exclude": ["lib/vendor/**"]},
        status=201,
    )
    return project, a, a_sign, b, b_sign


# ---------------------------------------------------------------------------
# F1: a zone slug stops at the zone's excludes
# ---------------------------------------------------------------------------


def test_release_by_zone_slug_leaves_a_path_claim_the_zone_excludes(live, clock):
    project, a, a_sign, b, b_sign = _zone_with_exclude(live, "exclrel")
    released = call("crew_claim", action="release", zone="lib")
    assert released == "RELEASED path lib/sales.ts (exclusive, active) → released", released
    assert _states(live, project, "c.path_glob = 'lib/vendor/x.ts'") == [("lib/vendor/x.ts", "active", a_sign)]
    again = call("crew_claim", action="release", zone="lib")
    assert again.startswith(OLD_EMPTY), again
    assert "You hold: path lib/vendor/x.ts (exclusive, active" in again, again
    # the claim is still released by its own name
    assert call("crew_claim", action="release", paths=["lib/vendor/x.ts"]) == (
        "RELEASED path lib/vendor/x.ts (exclusive, active) → released"
    )


def test_handover_by_zone_slug_leaves_a_path_claim_the_zone_excludes(live, clock):
    project, a, a_sign, b, b_sign = _zone_with_exclude(live, "exclhand")
    offered = call("crew_claim", action="handover", zone="lib", to=f"@{b_sign}")
    assert offered.startswith(f"OFFERED path lib/sales.ts (exclusive, active) to {b_sign}"), offered
    assert "lib/vendor" not in offered, offered
    assert _states(live, project, "c.path_glob = 'lib/vendor/x.ts'") == [("lib/vendor/x.ts", "active", a_sign)]


def test_adopt_by_zone_slug_leaves_a_reserved_path_claim_the_zone_excludes(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _zone_with_exclude(live, "excladopt")
    _set_claims(live, project, "path_glob = 'lib/vendor/x.ts'", state="reserved", reserve_reason="baton", reserved_for=None)
    use(b)
    writes = _writes(monkeypatch)
    out = call("crew_claim", action="adopt", zone="lib")
    assert out == "NOT ADOPTED zone lib: nothing is reserved there.", out
    assert writes == [], writes
    assert _states(live, project, "c.path_glob = 'lib/vendor/x.ts'") == [("lib/vendor/x.ts", "reserved", a_sign)]


def test_a_glob_claim_inside_an_excluded_folder_is_outside_the_zone():
    covered = crew_mod._path_claim_covered
    excluded = ["lib/vendor/**"]
    assert covered({"path_glob": "lib/sales.ts"}, None, ["lib/**"], excluded)
    assert not covered({"path_glob": "lib/vendor/x.ts"}, None, ["lib/**"], excluded)
    assert not covered({"path_glob": "lib/vendor/**"}, None, ["lib/**"], excluded)
    assert not covered({"path_glob": "lib/vendor/a/*.ts"}, None, ["lib/**"], excluded)
    assert not covered({"path_glob": "LIB/Vendor/x.ts"}, None, ["lib/**"], excluded)  # as the overlap test: case-folded
    assert covered({"path_glob": "lib/**"}, None, ["lib/**"], excluded)  # wider than the exclude: still in the zone
    assert not covered({"path_glob": "lib/sales.ts", "zone_id": "z1"}, None, ["lib/**"], excluded)  # a zone claim


# ---------------------------------------------------------------------------
# F1: one file does not release a wider claim
# ---------------------------------------------------------------------------


def test_release_of_one_file_leaves_a_wider_claim_that_covers_it(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _pair(live, "wide")
    use(a)
    assert "GRANTED" in call("crew_claim", paths=["art/one.psd"])
    _set_claims(live, project, "path_glob = 'art/one.psd'", path_glob="art/**")
    writes = _writes(monkeypatch)
    out = call("crew_claim", action="release", paths=["art/one.psd"])
    assert out.startswith(OLD_EMPTY), out
    assert "You hold: path art/** (exclusive, active" in out, out
    assert writes == [], writes
    assert _states(live, project, "c.path_glob = 'art/**'") == [("art/**", "active", a_sign)]
    handed = call("crew_claim", action="handover", paths=["art/one.psd"], to=f"@{b_sign}")
    assert handed.startswith("NOT HANDED OVER: you hold no active claim on that."), handed
    # its own spelling and the folder above it both name it
    assert call("crew_claim", action="release", paths=["art"]) == "RELEASED path art/** (exclusive, active) → released"


def test_the_empty_answer_still_names_the_holder_of_a_wider_claim(live, clock):
    project, a, a_sign, b, b_sign = _pair(live, "widetheirs")
    use(a)
    assert "GRANTED" in call("crew_claim", paths=["art/one.psd"])
    _set_claims(live, project, "path_glob = 'art/one.psd'", path_glob="art/**")
    use(b)
    out = call("crew_claim", action="release", paths=["art/one.psd"])
    assert out.startswith(OLD_EMPTY), out
    assert f"path art/** (exclusive, active) is held by {a_sign}, not by you." in out, out


# ---------------------------------------------------------------------------
# F1: adopt by zone slug with the zone's baton and a path baton under it
# ---------------------------------------------------------------------------


def test_adopt_by_zone_slug_takes_the_zones_own_baton_first(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _pair(live, "twobaton")
    use(a)
    assert "GRANTED" in call("crew_claim", paths=["lib/sales.ts"])
    add_zone(live, project, "lib", "lib/**")
    assert "GRANTED" in call("crew_claim", paths=["lib/notes.md"])  # filed under the zone: a zone claim
    b_id = session_row(live, project, b_sign)["id"]
    _set_claims(
        live,
        project,
        "path_glob = 'lib/sales.ts'",
        state="reserved",
        reserve_reason="baton",
        reserved_for=b_id,
        baton_ref="refs/remembra/baton/cs_aaa/1",
    )
    _set_claims(
        live,
        project,
        "zone_id IS NOT NULL AND state = 'active'",
        state="reserved",
        reserve_reason="baton",
        reserved_for=b_id,
        baton_ref="refs/remembra/baton/cs_bbb/1",
    )
    zone_claim = live.crew_rows(
        "SELECT id FROM crew_claims WHERE crew_id = ? AND zone_id IS NOT NULL AND state = 'reserved'",
        (crew_id_of(live, project),),
    )[0]["id"]
    use(b)
    writes = _writes(monkeypatch)
    out = call("crew_claim", action="adopt", zone="lib")
    assert writes == [("POST", f"/claims/{zone_claim}/adopt")], (writes, out)
    assert out.startswith("ADOPTED zone lib: you now hold zone lib (exclusive, active"), out
    assert 'STILL RESERVED path lib/sales.ts (exclusive, reserved): adopt it on its own with paths=["lib/sales.ts"].' in out, out
    assert "batons with saved work match" not in out, out
    assert _states(live, project, "c.path_glob = 'lib/sales.ts'") == [("lib/sales.ts", "reserved", a_sign)]
    # the spelling the answer gives picks the path baton alone
    writes.clear()
    second = call("crew_claim", action="adopt", paths=["lib/sales.ts"])
    assert second.startswith("ADOPTED lib/sales.ts: you now hold path lib/sales.ts (exclusive, active"), second
    assert len(writes) == 1, writes


# ---------------------------------------------------------------------------
# F24: only the crew server's own sentence is printed
# ---------------------------------------------------------------------------

NGINX = (
    "<html><head><title>429 Too Many Requests</title></head><body><center><h1>429 Too Many Requests</h1>"
    "</center><hr><center>nginx</center></body></html>"
)
SAID = "Too many adopt requests for this session (6/hour). Retry in 3569s."
OTHER = " Other kinds of crew call are not limited by this."


@pytest.mark.parametrize(
    ("error", "message", "body", "expected"),
    [
        # a proxy's page is not printed
        ("http_429", NGINX, {}, "RATE LIMITED (crew)."),
        ("http_429", "upstream said no", {}, "RATE LIMITED (crew)."),
        # a JSON answer with a message is (slowapi's shape: {"error": "Rate limit exceeded: ..."} has none)
        ("http_429", "Slow down.", {"message": "Slow down."}, "RATE LIMITED (crew): Slow down."),
        ("http_429", "<b>Slow</b>", {"message": "<b>Slow</b>"}, "RATE LIMITED (crew)."),
        # a wait that is not a whole number
        ("rate_limited", "rate_limited", {"error": "rate_limited", "retry_after_s": 12.5}, "RATE LIMITED (crew): retry in 13s."),
        ("rate_limited", "", {"retry_after_s": 0.2}, "RATE LIMITED (crew): retry in 1s."),
        ("rate_limited", "", {"retry_after_s": 0}, "RATE LIMITED (crew): retry in 0s."),
        ("rate_limited", "", {"retry_after_s": -3}, "RATE LIMITED (crew)."),
        ("rate_limited", "", {"retry_after_s": True}, "RATE LIMITED (crew)."),
        ("rate_limited", "", {"retry_after_s": float("inf")}, "RATE LIMITED (crew)."),
        # one wait, the field's, when the sentence carries another number
        (
            "rate_limited",
            SAID,
            {"message": SAID, "retry_after_s": 3500},
            "RATE LIMITED (crew): Too many adopt requests for this session (6/hour). Retry in 3500s (about 58 min)." + OTHER,
        ),
        (
            "rate_limited",
            SAID,
            {"message": SAID, "retry_after_s": 3569.4},
            "RATE LIMITED (crew): Too many adopt requests for this session (6/hour). Retry in 3570s (about 60 min)." + OTHER,
        ),
        # no field: the sentence's own wait stands, once
        ("rate_limited", SAID, {"message": SAID}, f"RATE LIMITED (crew): {SAID}" + OTHER),
    ],
)
def test_a_429_prints_only_a_crew_sentence_and_one_wait(error, message, body, expected):
    assert error_text(CrewApiError(429, error, message, body), "crew") == expected


def test_a_proxy_429_through_the_http_layer_reads_plain():
    import httpx

    res = httpx.Response(429, text=NGINX, headers={"content-type": "text/html"})
    assert error_text(crew_mod._error_from(res), "crew") == "RATE LIMITED (crew)."
    res = httpx.Response(429, json={"detail": {"error": "rate_limited", "message": SAID, "retry_after_s": 3569}})
    assert error_text(crew_mod._error_from(res), "crew") == (
        "RATE LIMITED (crew): Too many adopt requests for this session (6/hour). Retry in 3569s (about 59 min)." + OTHER
    )


# ---------------------------------------------------------------------------
# F40: the gate's literal-path equality follows the volume's case rule
# ---------------------------------------------------------------------------

ROUTE = "routes/[id]/page.tsx"


def _snap(ci: bool) -> dict:
    snap = snapshot()
    snap["claims"].append({**find(snap["claims"], "clm_cafe"), "id": "clm_route", "zone_id": None, "path_glob": ROUTE})
    for co in snap["checkouts"]:
        co["case_insensitive"] = ci
    return snap


def test_a_bracket_route_claim_blocks_the_other_spelling_on_a_case_insensitive_volume():
    v = run("Write", {"file_path": "/w/yaadbooks-c/Routes/[ID]/page.tsx", "content": "x"}, snap=_snap(True))
    assert (v.rule, v.decision) == (9, "deny"), (v.rule, v.decision, v.reason)


def test_a_bracket_route_claim_is_exact_on_a_case_sensitive_volume():
    v = run("Write", {"file_path": "/w/yaadbooks-c/Routes/[ID]/page.tsx", "content": "x"}, snap=_snap(False))
    assert v.rule != 9, (v.rule, v.decision, v.reason)
