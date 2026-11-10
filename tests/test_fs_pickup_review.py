"""F48 review follow-up: a handoff closed again is a new version nobody has been served yet.

The trail must not say it was picked up because an earlier version was: that
pickup is reported under ``earlier_version_picked_up_by`` (with the version
that was served), and ``picked_up_by`` names readers of this version only.
Also: the doctor's trail read line counts one row as one row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from remembra.marshal import doctor
from remembra.marshal.signals import _trail as doctor_trail
from remembra.marshal.signals import parse_time
from tests.agent_api_harness import build_api
from tests.marshal_fixtures import NOW, FakeHome, FakeTrail, entry


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


def _brief(api: dict[str, Any], agent: str, project: str | None = "widget") -> dict[str, Any]:
    params = {"agent_id": agent}
    if project:
        params["project_id"] = project
    res = api["http"].get("/api/v1/session/brief", params=params)
    assert res.status_code == 200, res.text
    return dict(res.json())


def _trail_items(api: dict[str, Any], project: str = "widget") -> list[dict[str, Any]]:
    res = api["http"].get("/api/v1/trail", params={"project_id": project})
    assert res.status_code == 200, res.text
    return list(res.json()["items"])


def _backdate_pickups(api: dict[str, Any], hours: float) -> None:
    async def _go() -> None:
        at = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None).isoformat()
        await api["app"].state.db.conn.execute("UPDATE relay_pickups SET picked_up_at = ?", (at,))
        await api["app"].state.db.conn.commit()

    api["http"].portal.call(_go)


# ---------------------------------------------------------------------------
# F48: a re-closed handoff is not "picked up" by a reader of the old version
# ---------------------------------------------------------------------------


def test_a_reclosed_handoff_is_not_reported_as_picked_up(api):
    first = _close(api, agent="codex", session="s1")
    _brief(api, "claude-desktop")
    _backdate_pickups(api, 2)
    second = _close(api, agent="codex", session="s1", todos_open=["a thing nobody has read"])
    assert second != first

    items = _trail_items(api)
    assert [i["id"] for i in items] == [second]
    row = items[0]
    assert row["picked_up_by"] == []  # nobody was served this version
    earlier = row["earlier_version_picked_up_by"]
    assert [(p["agent_id"], p["handoff_id"]) for p in earlier] == [("claude-desktop", first)]
    # The earlier pickup is from before this handoff existed: that is why it is not a pickup of it.
    assert parse_time(earlier[0]["picked_up_at"]) < parse_time(row["created_at"])

    # The doctor reads the same rows: the handoff still waits for its reader.
    signals = doctor_trail(items)
    assert signals["handoffs"] == (("codex", parse_time(row["created_at"]), ()),)
    assert signals["pickups_by"] == {}


def test_a_reclosed_handoff_is_picked_up_once_its_own_version_is_read(api):
    first = _close(api, agent="codex", session="s1")
    _brief(api, "claude-desktop")
    second = _close(api, agent="codex", session="s1", todos_open=["again"])
    _brief(api, "claude-code")
    row = _trail_items(api)[0]
    assert [(p["agent_id"], p["handoff_id"]) for p in row["picked_up_by"]] == [("claude-code", second)]
    assert [(p["agent_id"], p["handoff_id"]) for p in row["earlier_version_picked_up_by"]] == [("claude-desktop", first)]
    # The 7-day count still takes the session's handoff once, whichever version was read.
    summary = api["http"].get("/api/v1/trail/summary", params={"days": 7}).json()["pickups"]
    assert summary["handoffs_read"] == 1


def test_a_handoff_never_closed_again_has_no_earlier_pickups(api):
    handoff = _close(api, agent="codex", session="s1")
    _brief(api, "claude-desktop")
    row = _trail_items(api)[0]
    assert [(p["agent_id"], p["handoff_id"]) for p in row["picked_up_by"]] == [("claude-desktop", handoff)]
    assert row["earlier_version_picked_up_by"] == []


# ---------------------------------------------------------------------------
# F48: the doctor's read line
# ---------------------------------------------------------------------------


def _wired(tmp_path: Path) -> FakeHome:
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.claude_mcp()
    fh.codex_mcp()
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    return fh


def _trail_read(tmp_path: Path, items: list[dict[str, Any]]) -> str:
    fh = _wired(tmp_path)
    trail = FakeTrail(agents={"codex": {"handoffs": 1, "sessions_7d": 1, "daily": [0] * 6 + [1]}}, items=items)
    report = doctor.run(
        fh.home, None, True, environ=fh.environ(), transport=trail.transport, which=fh.which, now=NOW, cwd=fh.root
    )
    return next(r["result"] for r in report.json()["reads"] if r["what"] == "trail")


def test_doctor_trail_read_line_counts_one_row_as_one(tmp_path: Path):
    line = _trail_read(tmp_path, [entry("codex", "handoff", NOW - 600)])
    assert line.startswith("last 100 rows: 1 read (1 handoff or checkpoint;")
    assert "1 handoffs" not in line


def test_doctor_trail_read_line_keeps_the_plural(tmp_path: Path):
    line = _trail_read(tmp_path, [entry("codex", "handoff", NOW - 600), entry("codex", "checkpoint", NOW - 900)])
    assert line.startswith("last 100 rows: 2 read (2 handoffs and checkpoints;")
