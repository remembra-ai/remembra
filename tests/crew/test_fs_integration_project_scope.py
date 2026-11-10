"""F5 and F6 together (fault survey 2026-11-10): one rule for which project a crew write acts in.

F5 gave the six writing crew tools ``project_id`` / ``root_path`` for one call and ends a stdio answer with
``(project <id>)``. F6 refuses a write that names no project once the MCP session chose several, and ends
every write's answer with ``(in CREW <project> as <callsign>)``. Integrated, a project is *chosen* by
``session_brief``, ``crew_status`` or a write that named none; a write that named its project chose nothing.
Every test goes through ``FastMCP.call_tool`` against a live API, as a stdio server.
"""

from __future__ import annotations

import re

import pytest

from tests.crew.mcp_support import live_server
from tests.crew.test_fs_project_resolution import (
    OWNER_EMAIL,
    as_json,
    call,
    handoff_of,
    mcp_env,  # noqa: F401 - fixture
    messages,
    project_name,
    sessions,
    stdio,  # noqa: F401 - fixture
    use,
)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-integration-scope")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


def test_a_stdio_write_names_its_crew_and_ends_with_its_project(live, stdio, monkeypatch):  # noqa: F811
    project = project_name("both")
    use(live, "claude-desktop", "sess-both-lines", project_name("cfg"), monkeypatch)
    lines = call("crew_say", body="one note", kind="note", project_id=project).split("\n")
    assert lines[0].startswith("SENT msg_"), lines
    assert re.fullmatch(rf"\(in CREW {project} as [a-z][a-z0-9-]*\)", lines[-2]), lines
    assert lines[-1] == f"(project {project})", lines


def test_a_named_write_chooses_nothing_but_two_chosen_projects_refuse_an_unnamed_one(live, stdio, monkeypatch):  # noqa: F811
    a, b, c = project_name("chosena"), project_name("chosenb"), project_name("named")
    use(live, "claude-desktop", "sess-chosen", project_name("cfg"), monkeypatch)
    assert as_json(call("session_brief", project_id=a, compact=True))["project_id"] == a
    # conversation two names its project on each write: that chooses nothing, so conversation one's
    # unnamed calls still go to the one project this session chose
    assert call("crew_say", body="named", kind="note", project_id=c).startswith("SENT msg_")
    assert call("crew_say", body="unnamed", kind="note").rstrip().endswith(f"(project {a})")
    assert as_json(call("store_memory", content="The importer retries three times."))["project_id"] == a
    # a second chosen project (another conversation's crew_status): nothing unnamed may guess any more
    assert call("crew_status", project_id=b).startswith(f"CREW {b} ")
    refused = call("crew_say", body="where does this go", kind="note")
    assert refused.startswith("CREW: ") and "project_id" in refused and a in refused and b in refused, refused
    assert c not in refused, refused  # the project that was only named is not offered as a choice
    assert "(project " not in refused and "(in CREW " not in refused, refused  # nothing was written anywhere
    stored = as_json(call("store_memory", content="The exporter writes one file per day."))
    assert stored["status"] == "error" and a in stored["error"] and b in stored["error"], stored
    closed = as_json(call("close_session", summary="done", facts={"branch": "main"}))
    assert closed["status"] == "error" and "project_id" in closed["error"], closed
    assert messages(live, a) == ["unnamed"] and messages(live, b) == [] and messages(live, c) == ["named"]
    # naming the project still works for all three, and the close ends that project's seat only
    assert call("crew_say", body="for a", kind="note", project_id=a).rstrip().endswith(f"(project {a})")
    done = as_json(call("close_session", summary="done", facts={"branch": "main"}, project_id=a))
    assert (done["status"], done["project_id"], done["project_source"]) == ("ok", a, "named"), done
    assert done["crew"]["session_left"] is True, done
    assert [r["state"] for r in sessions(live, a)] == ["ended"]
    assert [r["state"] for r in sessions(live, b)] != ["ended"] and [r["state"] for r in sessions(live, c)] != ["ended"]
    assert handoff_of(live, b) is None and handoff_of(live, c) is None


def test_a_write_naming_its_folder_is_not_refused_when_two_projects_were_chosen(live, stdio, monkeypatch, tmp_path):  # noqa: F811
    a, b = project_name("foldera"), project_name("folderb")
    folder = tmp_path / project_name("checkout")
    folder.mkdir()
    use(live, "codex", "sess-folder", "default", monkeypatch)
    assert call("crew_status", project_id=a).startswith(f"CREW {a} ")
    assert call("crew_status", project_id=b).startswith(f"CREW {b} ")
    out = call("crew_checkpoint", files_changed=["notes.md"], summary="wrote notes", root_path=str(folder))
    assert out.startswith("CHECKPOINT ckp_") and out.rstrip().endswith(f"(project {folder.name})"), out
    assert f"(in CREW {folder.name} as " in out, out
    assert call("crew_status").startswith(f"CREW {b} ")  # the named write moved nothing


def test_a_status_that_only_looks_chooses_nothing(live, stdio, monkeypatch):  # noqa: F811
    a, b = project_name("looka"), project_name("lookb")
    use(live, "claude-desktop", "sess-look", project_name("cfg"), monkeypatch)
    assert call("crew_status", project_id=a).startswith(f"CREW {a} ")
    looked = call("crew_status", project_id=b, join=False)
    assert looked.startswith(f"CREW {b}") and "NOT JOINED (look only)" in looked, looked
    assert sessions(live, b) == []
    assert call("crew_say", body="still a", kind="note").rstrip().endswith(f"(project {a})")
    assert as_json(call("store_memory", content="The cache is rebuilt on deploy."))["project_id"] == a
