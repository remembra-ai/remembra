"""F49: a report refused for task ownership has to say what the task's state is and what to do next.

A forked Codex thread joined as a new crew session after the task's owner had closed. The task was
stalled with nothing reserved (it had no zones), so by design only a person can hand it over. Yet
every signal told the new session otherwise: its claims tagged with the task were GRANTED without a
word, the instructions said "When a task is done call crew_report", and the refusal named no next
step. An owner who released its own task was told to "start it before reporting", which is refused.

The ownership rule itself is unchanged (only the owner reports; a task with nothing reserved is
handed over by a person). These tests pin the guidance around it.

Every MCP test goes through ``FastMCP.call_tool`` into the real MCP server module and a live API
(``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

import pytest

import remembra.mcp.server as server
from remembra.crew import schemas as S
from remembra.crew.tasks import Caller, CrewServiceError
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewBridge
from tests.crew.mcp_support import live_server
from tests.crew.test_crew_reports import Env
from tests.crew.test_mcp_crew import (
    OWNER_EMAIL,
    Clock,
    _two_agents,
    agent,
    as_json,
    assert_clean,
    call,
    callsign_in,
    crew_id_of,
    project_name,
    use,
)
from tests.crew.wp6_support import CREW, open_db, seed_crew, seed_session

REFUSED = "REPORT NOT ACCEPTED for T-1: Only the session that owns T-1 reports on it."


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-task-report")) as s:
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


def _owner_left(live, prefix: str) -> tuple[str, str]:
    """codex-1 creates and starts a task without zones, then closes; a second Codex thread joins.

    Returns ``(project, owner callsign)`` with the successor's client in use."""
    project = project_name(prefix)
    a = agent(live, "codex", f"{project}-a", project)
    b = agent(live, "codex", f"{project}-b", project)
    use(a)
    a_sign = callsign_in(call("crew_status", project_id=project))
    assert call("crew_task", action="create", title="Places route").startswith("CREATED T-1")
    assert call("crew_task", action="start", task="T-1") == "STARTED T-1 (in_progress)."
    closed = as_json(call("close_session", summary="handing over", end_reason="handoff", project_id=project))
    assert closed["crew"]["tasks_stalled"] == 1 and closed["crew"]["claims_reserved"] == 0, closed
    use(b)
    assert callsign_in(call("crew_status", project_id=project)) != a_sign
    return project, a_sign


# ---------------------------------------------------------------------------
# The reported case: the owner left, nothing is reserved, a successor reports
# ---------------------------------------------------------------------------


def test_refused_report_on_a_stalled_task_names_its_state_and_the_ways_forward(live, clock):
    project, a_sign = _owner_left(live, "f49stalled")
    out = call("crew_report", task="T-1", sections={"done": ["route works"]})
    assert out.startswith(REFUSED), out  # the rule and its first sentence are unchanged
    assert f"T-1 is stalled: its owner {a_sign} left." in out, out
    assert 'crew_claim(action="adopt", task="T-1")' in out, out
    assert 'crew_task(action="start", task="T-1")' in out, out
    assert "only a human can hand it over" in out, out
    assert 'crew_task(action="create"' in out and "crew_checkpoint" in out, out
    assert_clean(out)
    # nothing was written and the task did not move
    crew = crew_id_of(live, project)
    assert live.crew_rows("SELECT status FROM crew_tasks WHERE crew_id = ?", (crew,)) == [{"status": "stalled"}]
    # the only report is the one the owner's close wrote
    reports = live.crew_rows(
        "SELECT r.kind, s.callsign FROM crew_reports r JOIN crew_sessions s ON s.id = r.session_id WHERE r.crew_id = ?", (crew,)
    )
    assert [r["callsign"] for r in reports] == [a_sign], reports


def test_the_way_forward_the_refusal_names_works(live, clock):
    """What the refusal tells the agent to do has to be accepted: its own task, started and reported on."""
    _owner_left(live, "f49own")
    assert call("crew_report", task="T-1", sections={"done": ["x"]}).startswith(REFUSED)
    # the adopt it names is refused for a task with nothing reserved, exactly as the text warned
    assert call("crew_claim", action="adopt", task="T-1").startswith("NOT ADOPTED T-1:")
    assert call("crew_task", action="create", title="Places route, continued").startswith("CREATED T-2")
    assert call("crew_task", action="start", task="T-2") == "STARTED T-2 (in_progress)."
    out = call("crew_report", task="T-2", sections={"done": ["route works"]}, summary="continued from T-1")
    assert out.startswith("REPORT rpt_") and "for T-2: verdict" in out, out


def test_claims_tagged_with_a_task_the_caller_does_not_own_say_so(live, clock):
    _, a_sign = _owner_left(live, "f49tag")
    out = call("crew_claim", paths=["app/api/places/route.ts"], task="T-1")
    lines = out.splitlines()
    assert lines[0].startswith("GRANTED path app/api/places/route.ts (exclusive, epoch 1, "), out  # still granted
    note = next((ln for ln in lines if ln.startswith("NOTE: ")), None)
    assert note is not None, out
    assert "tagged T-1, but you do not own T-1" in note, note
    assert f"T-1 is stalled: its owner {a_sign} left." in note, note
    assert "crew_report on T-1 will be refused" in note, note
    assert_clean(out)


def test_a_live_owners_task_refusal_names_the_owner_and_no_adopt(live, clock):
    _, a, a_sign, b, _ = _two_agents(live, "f49live")
    use(a)
    call("crew_task", action="create", title="Export", zones=["reports"])
    call("crew_task", action="start", task="T-1")
    use(b)
    out = call("crew_report", task="T-1", sections={"done": ["mine now"]})
    assert out.startswith(REFUSED), out
    assert f"T-1 is in_progress, owned by {a_sign}." in out, out
    assert "adopt" not in out, out  # a live owner's task is not adopted
    assert f'crew_say(to="@{a_sign}"' in out, out
    assert_clean(out)
    tagged = call("crew_claim", paths=["docs/export.md"], task="T-1")
    assert "GRANTED path docs/export.md" in tagged and "NOTE: " in tagged, tagged
    assert f"T-1 is in_progress, owned by {a_sign}." in tagged, tagged


def test_a_ready_task_nobody_owns_says_start_it(live, clock):
    _, a, _, b, _ = _two_agents(live, "f49ready")
    use(a)
    call("crew_task", action="create", title="Export")
    use(b)
    out = call("crew_report", task="T-1", sections={"done": ["x"]})
    assert out.startswith(REFUSED), out
    assert 'T-1 is ready and has no owner: crew_task(action="start", task="T-1") makes it yours' in out, out
    assert call("crew_task", action="start", task="T-1") == "STARTED T-1 (in_progress)."
    assert call("crew_report", task="T-1", sections={"done": ["x"]}).startswith("REPORT rpt_")


def test_the_owner_gets_no_note_on_its_own_tagged_claims_or_accepted_report(live, clock):
    _, a, *_ = _two_agents(live, "f49mine")
    use(a)
    call("crew_task", action="create", title="Export")
    call("crew_task", action="start", task="T-1")
    out = call("crew_claim", paths=["docs/export.md"], task="T-1")
    assert out.startswith("GRANTED path docs/export.md") and "NOTE" not in out, out
    report = call("crew_report", task="T-1", sections={"done": ["x"]})
    assert report.startswith("REPORT rpt_") and "NOT ACCEPTED" not in report, report


def test_a_stale_task_cache_does_not_tell_the_new_owner_it_is_not_the_owner(live, clock):
    """The seat caches the task list; a task this session started through another client must be re-read."""
    project, a, *_ = _two_agents(live, "f49cache")
    use(a)
    call("crew_task", action="create", title="Export")
    assert "T-1 [ready" in call("crew_task", action="list")  # caches the row without an owner
    seat = next(s for seats in server._crew._seats.values() for s in seats.values() if s.project_id == project)
    task_id = live.crew_rows("SELECT id FROM crew_tasks WHERE crew_id = ?", (crew_id_of(live, project),))[0]["id"]
    live.api(
        "POST",
        f"/tasks/{task_id}/start",
        headers={"X-API-Key": live.state["key"], crew_mod.SESSION_HEADER: seat.token},
        json={},
        status=(200, 201),
    )
    out = call("crew_claim", paths=["docs/export.md"], task="T-1")
    assert out.startswith("GRANTED path docs/export.md") and "NOTE" not in out, out


# ---------------------------------------------------------------------------
# The owner of a stalled task: "start it before reporting" pointed at a refused action
# ---------------------------------------------------------------------------


def test_the_owner_of_a_released_task_is_told_to_take_it_back_and_that_works(live, clock):
    _, a, *_ = _two_agents(live, "f49released")
    use(a)
    call("crew_task", action="create", title="Invoice PDF", zones=["pos"])
    call("crew_task", action="start", task="T-1")
    assert call("crew_task", action="release", task="T-1") == "RELEASED T-1: now stalled."
    out = call("crew_report", task="T-1", sections={"done": ["x"]})
    assert out.startswith("REPORT NOT ACCEPTED for T-1: T-1 is stalled"), out
    assert "start it before reporting" not in out, out  # start refuses a stalled task
    assert 'crew_claim(action="adopt", task="T-1")' in out, out
    assert_clean(out)
    assert call("crew_claim", action="adopt", task="T-1").startswith("ADOPTED T-1")
    assert call("crew_report", task="T-1", sections={"done": ["x"]}).startswith("REPORT rpt_")


async def test_the_server_refusal_matches_what_each_status_allows(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    try:
        env = Env(db)
        s = await seed_session(db)
        t = await env.started(s)

        async def refusal(status: str) -> str:
            await db.conn.execute("UPDATE crew_tasks SET status = ? WHERE id = ?", (status, t["id"]))
            await db.conn.commit()
            with pytest.raises(CrewServiceError) as e:
                await env.reports.submit(CREW, t["id"], Caller.for_session(s), {"summary": status})
            assert (e.value.status, e.value.error) == (409, "invalid_transition")
            return str(e.value.message)

        assert await refusal("stalled") == "T-1 is stalled; adopt it back before reporting (a stalled task cannot be started)."
        assert await refusal("claimed") == "T-1 is claimed; start it before reporting."
        assert await refusal("ready") == "T-1 is ready; start it before reporting."
        assert await refusal("done") == "T-1 is done; it takes no more reports."
        assert await refusal("cancelled") == "T-1 is cancelled; it takes no more reports."
    finally:
        await db.close()


def test_an_old_server_refusal_still_gets_the_next_step(live, clock, monkeypatch):
    """A fixed client against a 0.17.5 server: the old sentences come back and the client adds the way forward."""
    _owner_left(live, "f49old")
    real = crew_mod.CrewHttp.call

    def old_server(self, method, path, **kw):
        if method == "POST" and path.endswith("/reports"):
            raise crew_mod.CrewApiError(409, "invalid_transition", "T-1 is stalled; start it before reporting.")
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", old_server)
    out = call("crew_report", task="T-1", sections={"done": ["x"]})
    assert out.startswith("REPORT NOT ACCEPTED for T-1: T-1 is stalled; start it before reporting."), out
    assert 'A stalled task is not started: crew_claim(action="adopt", task="T-1")' in out, out


def test_a_refusal_survives_a_failing_task_lookup(live, clock, monkeypatch):
    """The refusal is never replaced by an error from the extra read that fetches the task's state."""
    _owner_left(live, "f49lookup")
    call("crew_task", action="list")
    real = crew_mod.CrewHttp.call
    state = {"refused": False}

    def flaky(self, method, path, **kw):
        if method == "POST" and path.endswith("/reports"):
            state["refused"] = True
            return real(self, method, path, **kw)
        if state["refused"] and method == "GET" and path.endswith("/tasks"):
            raise crew_mod.CrewApiError(0, "unreachable", "server not reached")
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", flaky)
    out = call("crew_report", task="T-1", sections={"done": ["x"]})
    assert out.startswith(REFUSED), out
    assert "T-1 is stalled" in out, out  # falls back to the row it already had


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def test_instructions_tie_crew_report_to_the_callers_own_task():
    text = server.mcp.instructions or ""
    assert "When your task is done call crew_report." in text
    assert "When a task is done call crew_report" not in text
    assert len(text) <= S.TEXT_CAPS["mcp_instructions"]
    assert S.check_agent_text(text, "mcp_instructions") == []
