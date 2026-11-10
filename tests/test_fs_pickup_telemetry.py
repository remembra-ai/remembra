"""F48: pickup telemetry that means something.

Three faults, each through the real code path:

* A brief could not be read without recording a pickup. ``GET /session/brief``
  now takes ``record=false`` (the SDK's ``record=False``, the MCP tool's
  ``observe=True``) and says what it did in ``pickup_recorded``.
* The doctor counted pickups in one page of 100 merged trail rows (a few hours
  once crew rows fill it) and printed the reader-keyed number as "picked up N"
  beside 7-day counts. ``/trail/summary`` now counts the 7 days from
  ``relay_pickups`` itself, per reader and per author, and the doctor prints
  both directions, the window they cover and what a pickup is.
* A pickup vanished from the trail when its author closed the same session
  again (the handoff was superseded). The trail and the summary now follow the
  superseded chain to the session's current handoff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp.crew import CrewBridge
from remembra.marshal import doctor
from remembra.security.untrusted import unwrap_untrusted
from tests.agent_api_harness import build_api
from tests.marshal_fixtures import NOW, FakeHome, FakeTrail, entry, iso


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _close(api: dict[str, Any], agent: str = "codex", session: str = "s1", project: str = "widget", **facts: Any) -> str:
    res = api["http"].post(
        "/api/v1/session/close",
        json={
            "agent_id": agent,
            "session_id": session,
            "project_id": project,
            "facts": {"branch": "main", "next_step": "carry on", **facts},
        },
    )
    assert res.status_code == 200, res.text
    return str(res.json()["handoff_id"])


def _brief(api: dict[str, Any], agent: str | None, project: str = "widget", **params: Any) -> dict[str, Any]:
    query = {"project_id": project, **params}
    if agent:
        query["agent_id"] = agent
    res = api["http"].get("/api/v1/session/brief", params=query)
    assert res.status_code == 200, res.text
    return dict(res.json())


def _pickups(api: dict[str, Any]) -> list[dict[str, Any]]:
    async def _q() -> list[dict[str, Any]]:
        cursor = await api["app"].state.db.conn.execute("SELECT * FROM relay_pickups ORDER BY picked_up_at")
        return [dict(r) for r in await cursor.fetchall()]

    return api["http"].portal.call(_q)


def _summary(api: dict[str, Any]) -> dict[str, Any]:
    res = api["http"].get("/api/v1/trail/summary", params={"days": 7})
    assert res.status_code == 200, res.text
    return dict(res.json())


def _trail(api: dict[str, Any], project: str = "widget") -> dict[str, dict[str, Any]]:
    res = api["http"].get("/api/v1/trail", params={"project_id": project})
    assert res.status_code == 200, res.text
    return {item["id"]: item for item in res.json()["items"]}


# ---------------------------------------------------------------------------
# Server: a brief that only looks
# ---------------------------------------------------------------------------


def test_observe_only_brief_records_no_pickup(api):
    handoff = _close(api, agent="codex")
    brief = _brief(api, "claude-desktop", record="false")
    assert brief["handoff"]["id"] == handoff  # the same brief is served
    assert brief["pickup_recorded"] is False
    assert _pickups(api) == []
    assert _trail(api)[handoff]["picked_up_by"] == []


def test_default_brief_still_records_and_says_so(api):
    _close(api, agent="codex")
    assert _brief(api, "claude-desktop")["pickup_recorded"] is True
    assert _brief(api, "claude-desktop")["pickup_recorded"] is False  # the same reader again: nothing new
    assert _brief(api, "claude-desktop", record="true", session_id="sess-a")["pickup_recorded"] is True
    assert _brief(api, "codex")["pickup_recorded"] is False  # the author reading its own handoff
    assert [r["reader_session"] for r in _pickups(api)] == ["", "sess-a"]


# ---------------------------------------------------------------------------
# Server: pickups survive a re-close and are counted over the 7 days
# ---------------------------------------------------------------------------


def test_a_pickup_survives_the_author_closing_again(api):
    first = _close(api, agent="codex", session="s1")
    _brief(api, "claude-desktop")
    second = _close(api, agent="codex", session="s1", todos_open=["one more thing"])
    assert second != first
    trail = _trail(api)
    assert first not in trail  # superseded: not listed any more
    assert trail[second]["picked_up_by"] == []  # nobody was served the new version
    picks = trail[second]["earlier_version_picked_up_by"]
    assert [p["agent_id"] for p in picks] == ["claude-desktop"]
    assert picks[0]["picked_up_at"] and picks[0]["handoff_id"] == first  # the version that was read


def test_summary_counts_pickups_per_reader_and_per_author(api):
    _close(api, agent="codex", session="s1")
    _brief(api, "claude-desktop")
    _close(api, agent="codex", session="s1", todos_open=["again"])  # supersedes the handoff that was read
    _brief(api, "claude-desktop")  # the same session's handoff, read again: still one handoff
    _close(api, agent="claude-code", session="c1", project="other")
    _brief(api, "codex", project="other")
    _brief(api, "claude-desktop", project="other", record="false")  # observe-only: never counted

    pickups = _summary(api)["pickups"]
    assert pickups["days"] == 7 and pickups["handoffs_read"] == 2
    assert "Last session" in pickups["definition"] and "not show" in pickups["definition"]
    readers = {r["agent_id"]: r for r in pickups["readers"]}
    assert readers["claude-desktop"]["handoffs_read"] == 1 and readers["codex"]["handoffs_read"] == 1
    assert readers["claude-desktop"]["last_read_at"] and readers["claude-desktop"]["continued"] == 0
    authors = {a["agent_id"]: a for a in pickups["authors"]}
    assert authors["codex"]["handoffs_read_by_others"] == 1
    assert authors["claude-code"]["handoffs_read_by_others"] == 1


def test_summary_says_whether_the_reader_carried_on(api):
    _close(api, agent="codex", session="s1")
    _brief(api, "claude-code")
    _brief(api, "cursor")
    _close(api, agent="claude-code", session="c1", todos_open=["continued codex's work"])
    readers = {r["agent_id"]: r for r in _summary(api)["pickups"]["readers"]}
    assert readers["claude-code"]["continued"] == 1  # wrote a handoff in that project afterwards
    assert readers["cursor"]["continued"] == 0  # read it and left nothing


def test_summary_with_no_pickups_is_empty_not_missing(api):
    _close(api, agent="codex")
    pickups = _summary(api)["pickups"]
    assert pickups["handoffs_read"] == 0 and pickups["readers"] == [] and pickups["authors"] == []


def test_summary_pickups_respect_a_project_restricted_key(api):
    from remembra.auth.middleware import AuthenticatedUser, get_current_user

    _close(api, agent="codex", project="widget")
    _brief(api, "claude-code", project="widget")
    _close(api, agent="codex", session="s2", project="secret")
    _brief(api, "claude-code", project="secret")
    user = AuthenticatedUser(user_id="default_user", api_key_id="k1", rate_limit_tier="standard", project_ids=["widget"])
    api["app"].dependency_overrides[get_current_user] = lambda: user
    pickups = _summary(api)["pickups"]
    assert pickups["handoffs_read"] == 1
    assert [(r["agent_id"], r["handoffs_read"]) for r in pickups["readers"]] == [("claude-code", 1)]


# ---------------------------------------------------------------------------
# SDK and MCP tool
# ---------------------------------------------------------------------------


def test_sdk_brief_sends_record_only_when_asked(api):
    client: Memory = api["make_client"](project="widget", agent_id="claude-desktop")
    _close(api, agent="codex")
    seen: list[dict[str, Any]] = []
    real = client._request

    def spy(method: str, path: str, **kwargs: Any) -> Any:
        seen.append(dict(kwargs.get("params") or {}))
        return real(method, path, **kwargs)

    client._request = spy  # type: ignore[method-assign]
    looked = client.session_brief(record=False)
    assert seen[-1]["record"] == "false" and looked["pickup_recorded"] is False
    assert _pickups(api) == []
    client.session_brief()
    assert "record" not in seen[-1]  # nothing new on the wire by default: an older server is asked as before
    assert len(_pickups(api)) == 1


@pytest.fixture()
def mcp_env(api, monkeypatch):
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "claude-desktop")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "alpha")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "sess-mcp")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    # a fresh server process: the crew bridge also remembers which projects the session was pointed at
    monkeypatch.setattr(server, "_crew", CrewBridge())
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    import json

    return dict(json.loads(unwrap_untrusted(raw)))


def test_mcp_observe_brief_writes_nothing_and_keeps_the_session_project(mcp_env):
    handoff = _close(mcp_env, agent="codex", project="widget")
    out = _j(server.session_brief(project_id="widget", observe=True))
    assert out["handoff_id"] == handoff and out["pickup_recorded"] is False and out["observe"] is True
    assert _pickups(mcp_env) == []
    # An audit's look at another project must not redirect where this session closes and stores.
    assert server._session_project() == {}
    compact = _j(server.session_brief(project_id="widget", observe=True, compact=True))
    assert compact["pickup_recorded"] is False and compact["observe"] is True
    assert _pickups(mcp_env) == []

    out = _j(server.session_brief(project_id="widget"))
    assert out["pickup_recorded"] is True and out["observe"] is False
    assert len(_pickups(mcp_env)) == 1
    assert server._session_project().get("project_id") == "widget"


def test_mcp_observe_brief_against_an_older_server_says_it_could_not_observe(mcp_env, monkeypatch):
    """A 0.17.5 server ignores ``record=false`` and returns no ``pickup_recorded``: say so, never claim observe-only."""

    class OldServerClient:
        project = "alpha"
        agent_id = "claude-desktop"

        def session_brief(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["record"] is False
            brief: dict[str, Any] = {"project_id": "widget", "agent_id": "claude-desktop", "handoff": {"id": "h1"}}
            return {**brief, "rendered": "x", "warnings": []}

    monkeypatch.setattr(server, "_get_client", lambda: OldServerClient())
    out = _j(server.session_brief(project_id="widget", observe=True, compact=True))
    assert out["pickup_recorded"] is None
    assert any("observe" in w and "pickup" in w for w in out["warnings"]), out["warnings"]
    assert server._session_project() == {}


def test_mcp_brief_description_says_a_default_brief_records_a_pickup():
    doc = server.session_brief.__doc__ or ""
    assert "observe" in doc and "records a pickup" in doc


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class SummaryTrail(FakeTrail):
    """FakeTrail whose summary also carries the server's 7-day pickup counts (0.17.7+ servers)."""

    def __init__(self, *args: Any, pickups: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pickups = pickups

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        if request.url.path == "/api/v1/trail/summary" and response.status_code == 200 and self.pickups is not None:
            import json

            body = json.loads(response.content)
            body["pickups"] = self.pickups
            return httpx.Response(200, json=body)
        return response


def _crew_row(at: float, agent: str = "codex") -> dict[str, Any]:
    return {"id": f"crew-{int(at)}", "agent_id": agent, "memory_type": "crew_checkpoint", "created_at": iso(at)}


def _wired(tmp_path: Path) -> FakeHome:
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.claude_mcp()
    fh.codex_mcp()
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    return fh


def _flat(report: doctor.Report) -> str:
    """The slip as one line of words (wrapping and the rail removed), for sentence assertions."""
    return " ".join(report.text().replace("┊", " ").split())


def _reads(report: doctor.Report) -> dict[str, str]:
    return {r["what"]: r["result"] for r in reversed(report.json()["reads"])}  # the first read of each kind


def _run(fh: FakeHome, trail: FakeTrail) -> doctor.Report:
    return doctor.run(fh.home, None, True, environ=fh.environ(), transport=trail.transport, which=fh.which, now=NOW, cwd=fh.root)


AGENTS = {
    "claude-code": {"handoffs": 9, "sessions_7d": 5, "daily": [1, 1, 1, 1, 1, 1, 1], "last_active": iso(NOW - 5 * 3600)},
    "codex": {"handoffs": 60, "sessions_7d": 35, "daily": [17] * 7, "last_active": iso(NOW - 600)},
}


def test_doctor_reports_seven_day_pickups_when_crew_rows_fill_the_window(tmp_path: Path):
    """The reported slip: 100 crew rows in the page, so the window can only say 0. The server's count is used."""
    fh = _wired(tmp_path)
    window = [_crew_row(NOW - 60 * i) for i in range(1, 97)] + [entry("codex", "checkpoint", NOW - 6000 - i) for i in range(4)]
    trail = SummaryTrail(
        agents=AGENTS,
        items=window,
        pickups={
            "days": 7,
            "handoffs_read": 18,
            "readers": [
                {"agent_id": "codex", "handoffs_read": 12, "continued": 9, "last_read_at": iso(NOW - 4.5 * 3600)},
                {"agent_id": "claude-code", "handoffs_read": 6, "continued": 2, "last_read_at": iso(NOW - 7200)},
            ],
            "authors": [
                {"agent_id": "codex", "handoffs_read_by_others": 6},
                {"agent_id": "claude-code", "handoffs_read_by_others": 12},
            ],
        },
    )
    report = _run(fh, trail)
    text = report.text()
    assert "0 pickups" not in text and "picked up 0" not in text
    flat = _flat(report)
    # The read lines say what the page really held, what the 7 days hold and what a pickup is.
    reads = _reads(report)
    assert reads["trail"] == "last 100 rows: 100 read (4 handoffs and checkpoints, 96 crew rows; covering 2h)"
    assert reads["pickups"].startswith("7d: 18 handoffs read by another agent · a pickup = the handoff was served as")
    assert "does not show the work was continued" in reads["pickups"]
    assert "served as" in flat and "does not show the work was continued" in flat
    # Both directions on the agent's own line, over the same 7 days as the counts beside them.
    assert "(35 handoffs, 6 read by another agent)" in flat
    assert "read 12 from other agents, carried on after 9" in flat
    data = report.json()
    codex = next(a for a in data["agents"] if a["name"] == "codex")["trail"]
    assert codex["handoffs_read_7d"] == 12 and codex["handoffs_read_by_others_7d"] == 6 and codex["continued_7d"] == 9
    assert codex["pickups"] == 0  # the window count is still there for older readers of the JSON
    assert codex["pickups_window"]["rows"] == 100 and codex["pickups_window"]["crew_rows"] == 96
    # No extra reads for it: the key check (which is the summary), the trail page, and claude-code's own
    # last entries (it is not in the page), exactly as before.
    assert [r.url.path for r in trail.requests] == ["/api/v1/trail/summary", "/api/v1/trail", "/api/v1/trail"]


def test_doctor_against_an_older_server_names_the_window_instead_of_a_bare_zero(tmp_path: Path):
    """A 0.17.5 server has no 7-day pickup count: the line says which rows the number covers."""
    fh = _wired(tmp_path)
    window = [_crew_row(NOW - 150 * i) for i in range(1, 97)] + [
        entry("codex", "checkpoint", NOW - 4 * 3600 - i) for i in range(4)
    ]
    report = _run(fh, FakeTrail(agents=AGENTS, items=window))
    flat = _flat(report)
    assert "0 pickups" not in flat and "picked up 0" not in flat
    reads = _reads(report)
    assert reads["trail"] == "last 100 rows: 100 read (4 handoffs and checkpoints, 96 crew rows; covering 4h)"
    assert reads["pickups"].startswith("in those rows: 0 handoffs read by another agent · a pickup = ")
    assert "trail 119 entries in 7d (35 handoffs) · read 0 from other agents in the last 100 rows (4h)" in flat
    codex = next(a for a in report.json()["agents"] if a["name"] == "codex")["trail"]
    assert codex["handoffs_read_7d"] is None and codex["handoffs_read_by_others_7d"] is None
    assert codex["pickups_window"] == {"rows": 100, "crew_rows": 96, "span_seconds": pytest.approx(4 * 3600 - 147, abs=5)}


def test_doctor_ignores_a_malformed_pickup_block(tmp_path: Path):
    fh = _wired(tmp_path)
    trail = SummaryTrail(
        agents=AGENTS,
        items=[entry("claude-code", "handoff", NOW - 3600, picked=[("codex", NOW - 1800)])],
        pickups={"days": 7, "handoffs_read": 3, "readers": [None, {"agent_id": "codex", "handoffs_read": "many"}], "authors": []},
    )
    report = _run(fh, trail)
    assert "read 1 from other agents in the last 100 rows" in _flat(report)  # falls back to the window
    assert _reads(report)["pickups"].startswith("in those rows: 1 handoff read by another agent")
    codex = next(a for a in report.json()["agents"] if a["name"] == "codex")["trail"]
    assert codex["handoffs_read_7d"] is None and codex["pickups"] == 1
