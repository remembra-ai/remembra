"""Fault survey F24: an adopt that hits the rate limit says what is limited and keeps what it did.

The server answers 429 with ``Too many adopt requests for this session (6/hour). Retry in Ns.``;
the MCP bridge printed only ``RATE LIMITED (crew): retry in Ns.``, and a 429 on the second claim
of one adopt call threw away the ADOPTED line of the first. A session taking back its own
reservation (released with ``baton=True``, parked idle) was charged against that same
6/hour takeover budget.

Live tests go through ``FastMCP.call_tool`` into the real bridge and a live API server with the
real crew rate limiter switched on (``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.crew import limits as limits_mod
from remembra.crew.limits import BucketSpec, CrewRateLimiter, set_crew_rate_limiter
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewApiError, CrewBridge, error_text
from tests.crew.mcp_support import LiveServer, live_server
from tests.crew.test_mcp_crew import Clock, add_zone, agent, assert_clean, call, callsign_in, project_name, use

OWNER_EMAIL = "owner@example.com"
SERVER_SAID = "Too many adopt requests for this session (6/hour). Retry in 3569s."


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-adopt-limit")) as s:
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
def limited(live, monkeypatch):
    """The crew rate limiter as production runs it: enabled, in-memory, the real bucket table."""
    monkeypatch.setattr(live.settings, "rate_limit_enabled", True)
    set_crew_rate_limiter(CrewRateLimiter("memory://"))
    yield
    set_crew_rate_limiter(None)


def _writes(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real_call = crew_mod.CrewHttp.call

    def call_spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method != "GET":
            seen.append((method, path))
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", call_spy)
    return seen


def _parked(live: LiveServer, prefix: str, paths: list[str]) -> tuple[str, Memory, Memory]:
    """``a`` claimed ``paths`` and left each as a baton; ``d`` joined afterwards, so the batons are offered to it."""
    project = project_name(prefix)
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    callsign_in(call("crew_status", project_id=project))
    add_zone(live, project, "pos", "src/app/pos/**")
    for p in paths:
        assert f"GRANTED path {p}" in call("crew_claim", paths=[p])
        assert call("crew_claim", action="release", paths=[p], baton=True).endswith("→ reserved")
    d = agent(live, "claude-desktop", f"{project}-d", project)
    use(d)
    call("crew_status", project_id=project)
    return project, a, d


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


def test_a_429_prints_what_the_server_said():
    e = CrewApiError(429, "rate_limited", SERVER_SAID, {"error": "rate_limited", "retry_after_s": 3569})
    text = error_text(e, "crew")
    assert text.startswith("RATE LIMITED (crew): Too many adopt requests for this session (6/hour)."), text
    assert "Retry in 3569s (about 59 min)." in text, text
    assert "Other kinds of crew call are not limited by this." in text, text


def test_a_short_wait_is_not_given_in_minutes():
    said = "Too many claims requests for this session (60/minute). Retry in 12s."
    e = CrewApiError(429, "rate_limited", said, {"retry_after_s": 12})
    text = error_text(e, "claim zone pos")
    assert text == (
        "RATE LIMITED (claim zone pos): Too many claims requests for this session (60/minute). Retry in 12s."
        " Other kinds of crew call are not limited by this."
    ), text


@pytest.mark.parametrize(
    ("message", "body", "expected"),
    [
        ("", {"retry_after_s": 30}, "RATE LIMITED (crew): retry in 30s."),  # an answer with no sentence: as before
        ("rate_limited", {"retry_after_s": 30}, "RATE LIMITED (crew): retry in 30s."),  # only the error code came back
        ("", {}, "RATE LIMITED (crew)."),
        ("Rate limit exceeded: 100 per 1 minute", {}, "RATE LIMITED (crew): Rate limit exceeded: 100 per 1 minute"),
        ("Slow down.", {"retry_after_s": 600}, "RATE LIMITED (crew): Slow down. Retry in 600s (about 10 min)."),
        ("Slow down.", {"retry_after_s": "soon"}, "RATE LIMITED (crew): Slow down."),
    ],
)
def test_a_429_without_the_crew_sentence_still_reads(message, body, expected):
    error = "rate_limited" if message in ("", "rate_limited") else "http_429"
    assert error_text(CrewApiError(429, error, message, body), "crew") == expected


# ---------------------------------------------------------------------------
# The adopt loop keeps what it did
# ---------------------------------------------------------------------------


def test_an_adopt_cut_short_by_the_limit_reports_what_was_adopted(live, clock, limited, monkeypatch):
    monkeypatch.setitem(limits_mod.CREW_RATE_BUCKETS, "adopt", BucketSpec(per_session="2/hour", per_user="30/hour"))
    paths = ["art/a.psd", "art/b.psd", "art/c.psd"]
    project, a, d = _parked(live, "cutshort", paths)
    writes = _writes(monkeypatch)
    got = call("crew_claim", action="adopt", paths=paths)
    lines = got.split("\n")
    assert lines[0].startswith("ADOPTED art/a.psd, art/b.psd, art/c.psd: you now hold path art/a.psd (exclusive, active)"), got
    assert "path art/b.psd (exclusive, active)" in lines[0] and "art/c.psd (exclusive" not in lines[0], got
    assert lines[1].startswith(
        "NOT ADOPTED path art/c.psd (exclusive, reserved): Too many adopt requests for this session (2/hour). Retry in "
    ), got
    assert "RATE LIMITED" not in got, got
    assert_clean(got)
    assert len([w for w in writes if w[1].endswith("/adopt")]) == 3, writes
    states = live.crew_rows(
        "SELECT c.path_glob, c.state, s.agent_id FROM crew_claims c JOIN crew_sessions s ON s.id = c.holder_session_id"
        " WHERE s.crew_id = (SELECT id FROM crews WHERE project_id = ?) AND c.state IN ('active', 'reserved')"
        " ORDER BY c.path_glob",
        (project,),
    )
    assert [(r["path_glob"], r["state"], r["agent_id"]) for r in states] == [
        ("art/a.psd", "active", "claude-desktop"),
        ("art/b.psd", "active", "claude-desktop"),
        ("art/c.psd", "reserved", "codex"),
    ], states

    # the whole call refused: the same sentence, and no further POST for the claims after the first refusal
    del writes[:]
    again = call("crew_claim", action="adopt", paths=["art/c.psd"])
    assert again.startswith("NOT ADOPTED art/c.psd: Too many adopt requests for this session (2/hour). Retry in "), again
    assert "about" in again and "min" in again, again
    assert len([w for w in writes if w[1].endswith("/adopt")]) == 1, writes
    # a plain claim is a different budget and still works
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")


def test_an_adopt_stops_asking_after_the_limit(live, clock, limited, monkeypatch):
    monkeypatch.setitem(limits_mod.CREW_RATE_BUCKETS, "adopt", BucketSpec(per_session="1/hour", per_user="30/hour"))
    paths = ["art/a.psd", "art/b.psd", "art/c.psd"]
    project, a, d = _parked(live, "stops", paths)
    writes = _writes(monkeypatch)
    got = call("crew_claim", action="adopt", paths=paths)
    assert got.count("NOT ADOPTED path art/") == 2, got
    assert "path art/c.psd (exclusive, reserved): not tried, the adopt limit was reached" in got, got
    assert len([w for w in writes if w[1].endswith("/adopt")]) == 2, writes  # a, then b refused; c is not sent


def test_a_task_adopt_refused_by_the_limit_names_the_task_and_the_limit(live, clock, limited, monkeypatch):
    monkeypatch.setitem(limits_mod.CREW_RATE_BUCKETS, "adopt", BucketSpec(per_session="1/hour", per_user="30/hour"))
    project = project_name("tasklimit")
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    add_zone(live, project, "pos", "src/app/pos/**")
    assert call("crew_task", action="create", title="POS split tender", zones=["pos"]).startswith("CREATED T-1 ")
    assert call("crew_task", action="start", task="T-1").startswith("STARTED T-1")
    assert "GRANTED path art/a.psd" in call("crew_claim", paths=["art/a.psd"])
    call("crew_claim", action="release", paths=["art/a.psd"], baton=True)
    assert call("crew_claim", action="release", task="T-1").startswith("RELEASED T-1: task is now stalled")
    d = agent(live, "claude-desktop", f"{project}-d", project)
    use(d)
    call("crew_status", project_id=project)
    assert call("crew_claim", action="adopt", paths=["art/a.psd"]).startswith("ADOPTED art/a.psd")  # the one allowed
    got = call("crew_claim", action="adopt", task="T-1")
    assert got.startswith("NOT ADOPTED T-1: Too many adopt requests for this session (1/hour). Retry in "), got
    assert "RATE LIMITED" not in got, got


# ---------------------------------------------------------------------------
# A session's own reservation is not a takeover
# ---------------------------------------------------------------------------


def test_taking_back_your_own_reservation_does_not_use_the_takeover_budget(live, clock, limited):
    """Codex parked a file with ``release baton=True`` and adopted it back, again and again: the seventh
    time in an hour answered RATE LIMITED (crew). The real 6/hour table is in force here."""
    project = project_name("ownretake")
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    assert "GRANTED path docs/route.md" in call("crew_claim", paths=["docs/route.md"])
    for i in range(8):
        assert call("crew_claim", action="release", paths=["docs/route.md"], baton=True).endswith("→ reserved"), i
        got = call("crew_claim", action="adopt", paths=["docs/route.md"])
        assert got.startswith("ADOPTED docs/route.md: you now hold path docs/route.md (exclusive, active)"), (i, got)


def test_releasing_your_own_reservation_needs_no_adopt(live, clock, limited, monkeypatch):
    project = project_name("noadopt")
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    writes = _writes(monkeypatch)
    for i in range(8):
        path = f"docs/n{i}.md"
        assert f"GRANTED path {path}" in call("crew_claim", paths=[path])
        assert call("crew_claim", action="release", paths=[path], baton=True).endswith("→ reserved")
        assert call("crew_claim", action="release", paths=[path]).endswith("→ released"), i
    assert not [w for w in writes if w[1].endswith("/adopt")], writes


def test_the_tool_text_tells_an_agent_how_to_take_back_or_free_its_own_reservation():
    doc = server.crew_claim.__doc__ or ""
    assert "reserved" in doc and "re-takes" in doc, doc


def test_against_a_server_that_limits_own_retakes_the_answer_points_to_claim_and_release(live, clock, monkeypatch):
    """Server 0.17.5 charges every adopt against the takeover budget; the client then says how to avoid adopt."""
    project = project_name("oldserver")
    a = agent(live, "codex", f"{project}-a", project)
    use(a)
    call("crew_status", project_id=project)
    assert "GRANTED path docs/route.md" in call("crew_claim", paths=["docs/route.md"])
    call("crew_claim", action="release", paths=["docs/route.md"], baton=True)
    real_call = crew_mod.CrewHttp.call

    def old_server(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path.endswith("/adopt"):
            raise CrewApiError(429, "rate_limited", SERVER_SAID, {"error": "rate_limited", "retry_after_s": 3569})
        return real_call(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", old_server)
    got = call("crew_claim", action="adopt", paths=["docs/route.md"])
    lines = got.split("\n")
    assert lines[0].startswith("NOT ADOPTED docs/route.md: Too many adopt requests for this session (6/hour)."), got
    assert "Retry in 3569s (about 59 min)." in lines[0], got
    assert lines[1].startswith("A reserved claim of your own needs no adopt: a plain crew_claim on it re-takes it"), got
    # and both ways out work against that server
    assert call("crew_claim", paths=["docs/route.md"]).startswith("GRANTED path docs/route.md"), "plain claim re-takes"
    call("crew_claim", action="release", paths=["docs/route.md"], baton=True)
    assert call("crew_claim", action="release", paths=["docs/route.md"]).endswith("→ released")
