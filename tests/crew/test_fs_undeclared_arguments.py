"""F22: an argument a tool does not declare is never silently dropped.

It is mapped to the declared name it means (``project`` to ``project_id``, ``open_work`` to
``todos_open``, ``note`` to ``reason``), checked (a project or location named on a crew tool must be
the crew this session is on), or refused in one line that names the key. Every call goes through
``FastMCP.call_tool`` into the real MCP server module and a live API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mcp.server.fastmcp.exceptions import ToolError

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from tests.crew.test_mcp_crew import (
    agent,
    as_json,
    crew_id_of,
    project_name,
    use,
)

# the live API and the fresh crew bridge, shared with the WP-11 suite
live = base.live
clock = base.clock


def call(name: str, **args: Any) -> str:
    """A tool's answer as text. An argument failure is an MCP error (``ToolError``): its text is the answer."""
    try:
        return base.call(name, **args)
    except ToolError as e:
        return str(e)


def _seated(live: Any, prefix: str, agent_id: str = "claude-desktop") -> str:
    project = project_name(prefix)
    use(agent(live, agent_id, f"{project}-s", project))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")
    return project


def _claims(live: Any, project: str) -> list[dict[str, Any]]:
    return live.crew_rows(
        "SELECT resource, reason, state FROM crew_claims WHERE crew_id = ? ORDER BY created_at", (crew_id_of(live, project),)
    )


# -- every tool -----------------------------------------------------------------------------


def test_every_tool_schema_forbids_additional_properties():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 31
    assert [t.name for t in tools if t.inputSchema.get("additionalProperties") is not False] == []


def test_an_undeclared_argument_is_refused_by_name_and_nothing_runs(live, clock):
    project = _seated(live, "unknown")
    assert call("crew_claim", zone="schema:main").startswith("GRANTED schema:main")
    out = call("crew_claim", action="release", claim_ids=["clm_01ABC"])
    assert out.startswith("INVALID ARGUMENTS: crew_claim does not take claim_ids"), out
    assert "zone=" in out and "paths=" in out and "task=" in out, out
    assert "Name what to release" not in out and "Nothing was done" in out, out
    assert "crew_claim takes: action, zone, paths, mode, task, to, baton, reason, wait_s, project_id, root_path." in out, out
    assert [c["state"] for c in _claims(live, project)] == ["active"]

    zones = call("crew_claim", zones=["pos", "reports"])
    assert zones.startswith("INVALID ARGUMENTS: crew_claim does not take zones"), zones
    trigger = call("crew_checkpoint", files_changed=[], summary="deployed", trigger="deploy")
    assert trigger.startswith("INVALID ARGUMENTS: crew_checkpoint does not take trigger"), trigger
    assert "commits" in trigger and "tests" in trigger, trigger
    assert live.crew_rows("SELECT id FROM crew_checkpoints WHERE crew_id = ?", (crew_id_of(live, project),)) == []
    # a value never comes back in the refusal
    secret = call("crew_say", body="hi", priority="SECRETVALUE")
    assert secret.startswith("INVALID ARGUMENTS: crew_say does not take priority") and "SECRETVALUE" not in secret, secret
    assert live.crew_rows("SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),)) == []


def test_an_undeclared_argument_set_to_null_asks_for_nothing_and_is_ignored(live, clock):
    _seated(live, "nulls")
    assert call("crew_claim", zone="schema:main", claim_ids=None, project=None).startswith("GRANTED schema:main")


def test_arguments_that_are_not_an_object_are_refused(clock):
    out = asyncio.run(server.mcp._tool_manager.call_tool("crew_status", None, convert_result=False))  # type: ignore[arg-type]
    assert not str(out).startswith("INVALID ARGUMENTS"), out  # no arguments at all is an ordinary call
    with pytest.raises(ToolError) as bad:
        asyncio.run(server.mcp._tool_manager.call_tool("crew_say", ["hi"], convert_result=False))  # type: ignore[arg-type]
    assert str(bad.value).startswith("INVALID ARGUMENTS: crew_say takes named arguments"), bad.value


# -- project -> project_id ------------------------------------------------------------------


def test_project_means_project_id_where_the_tool_has_one(live, clock):
    first = _seated(live, "alias")
    other = project_name("aliasother")
    out = call("crew_status", project=other)
    assert out.startswith(f"CREW {other} "), out
    assert call("crew_status", project=first, project_id=first).startswith(f"CREW {first} ")
    clash = call("crew_status", project=first, project_id=other)
    assert clash.startswith("INVALID ARGUMENTS:") and "project" in clash and "project_id" in clash, clash


def test_store_memory_stores_in_the_project_it_is_given(live, clock):
    home = _seated(live, "storehome")
    target = project_name("storetarget")
    by_id = json.loads(call("store_memory", content="The staging database is Postgres 16.", project_id=target))
    assert by_id["status"] == "stored", by_id
    by_alias = json.loads(call("store_memory", content="The cache layer is Redis 7.", project=target))
    assert by_alias["status"] == "stored", by_alias
    plain = json.loads(call("store_memory", content="The queue is SQS."))
    assert plain["status"] == "stored", plain
    in_target = {m["id"] for m in as_json(call("list_memories", project_id=target))["memories"]}
    in_home = {m["id"] for m in as_json(call("list_memories", project_id=home))["memories"]}
    assert {by_id["id"], by_alias["id"]} <= in_target and plain["id"] not in in_target
    assert plain["id"] in in_home and not ({by_id["id"], by_alias["id"]} & in_home)


def test_close_session_keeps_open_work_and_project(live, clock):
    _seated(live, "closehome")
    target = project_name("closetarget")
    out = as_json(
        call("close_session", summary="half done", open_work=["wire the retry queue", "write the migration"], project=target)
    )
    assert out["status"] == "ok" and out["project_id"] == target, out
    assert "wire the retry queue" in out["rendered"] and "write the migration" in out["rendered"], out["rendered"]


def test_send_to_inbox_tags_the_project_it_is_given(live, clock):
    _seated(live, "inboxproject")
    target = project_name("inboxtarget")
    sent = json.loads(call("send_to_inbox", to_agent="codex", subject="scoped", body="b", project=target))
    assert sent["ok"] is True, sent
    item = next(i for i in as_json(call("get_inbox", agent_id="codex"))["items"] if i["inbox_id"] == sent["inbox_id"])
    assert item["metadata"]["project_id"] == target, item
    # the inbox is per agent: a project on a read is refused, not ignored
    refused = call("get_inbox", project=target)
    assert refused.startswith("INVALID ARGUMENTS: get_inbox does not take project"), refused
    assert "per agent" in refused, refused


# -- a project or location named on a crew tool -----------------------------------------------


def test_a_crew_tool_runs_when_the_project_it_names_is_this_sessions_crew(live, clock):
    project = _seated(live, "same")
    assert call("crew_claim", zone="schema:main", project=project).startswith("GRANTED schema:main")
    assert call("crew_say", body="on it", project_id=project).startswith("SENT msg_")
    assert call("crew_checkpoint", files_changed=["a.py"], summary="x", project=project).startswith("CHECKPOINT ckp_")
    assert [c["resource"] for c in _claims(live, project)] == ["schema:main"]


def test_a_crew_tool_that_names_another_project_acts_there_for_that_call_only(live, clock):
    # A crew tool takes project_id for one call since F6 (tests/crew/test_fs_project_resolution.py), so the
    # project a call names, under either name, is where the call acts: it is never dropped for the session's crew.
    project = _seated(live, "here")
    other = project_name("elsewhere")
    out = call("crew_claim", zone="deploy:prod", project=other, reason="deploying")
    assert out.startswith("GRANTED deploy:prod") and f"(in CREW {other} as " in out, out
    assert _claims(live, project) == []
    assert [(c["resource"], c["reason"]) for c in _claims(live, other)] == [("deploy:prod", "deploying")]
    said = call("crew_say", body="for the other crew", project_id=other)
    assert said.startswith("SENT msg_") and f"(in CREW {other} as " in said, said
    assert live.crew_rows("SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),)) == []
    rows = live.crew_rows("SELECT body FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, other),))
    assert [r["body"] for r in rows] == ["for the other crew"]
    # the session's own crew is still where a call without a project goes
    mine = call("crew_claim", zone="schema:main")
    assert mine.startswith("GRANTED schema:main") and f"(in CREW {project} as " in mine, mine
    # the two names for one argument have to agree
    clash = call("crew_claim", zone="deploy:other", project=project, project_id=other)
    assert clash.startswith("INVALID ARGUMENTS: project_id and project are the same argument and differ"), clash
    assert [c["resource"] for c in _claims(live, other)] == ["deploy:prod"]
    assert [c["resource"] for c in _claims(live, project)] == ["schema:main"]


def test_a_location_named_on_a_crew_tool_is_checked_against_the_crew(live, clock):
    project = project_name("located")
    other = project_name("locatedother")
    headers = {"X-API-Key": live.state["key"]}
    for name in (project, other):
        live.api(
            "POST",
            "/projects/resolve",
            headers=headers,
            json={"git_remote": f"https://github.com/acme/{name}.git", "hint_project": name},
        )
    use(agent(live, "codex", f"{project}-s", "default"))
    assert call("crew_status", git_remote=f"git@github.com:acme/{project}.git").startswith(f"CREW {project} ")
    assert call("crew_claim", zone="schema:main", git_remote=f"git@github.com:acme/{project}.git").startswith(
        "GRANTED schema:main"
    )
    out = call("crew_claim", zone="schema:other", git_remote=f"git@github.com:acme/{other}.git")
    assert out.startswith("INVALID ARGUMENTS: crew_claim acts on this session's crew"), out
    assert project in out and other in out, out
    unknown = call("crew_claim", zone="schema:other", git_remote="git@github.com:acme/never-seen-anywhere.git")
    assert unknown.startswith("INVALID ARGUMENTS: crew_claim acts on this session's crew"), unknown
    assert [c["resource"] for c in _claims(live, project)] == ["schema:main"]


def test_a_crew_tool_that_names_a_project_before_any_crew_is_joined_acts_in_that_project(live, clock):
    use(agent(live, "claude-desktop", "nocrew-s", "default"))
    wanted = project_name("nocrew")
    out = call("crew_claim", zone="schema:main", project=wanted)
    assert out.startswith("GRANTED schema:main") and f"(in CREW {wanted} as " in out, out
    assert [c["resource"] for c in _claims(live, wanted)] == ["schema:main"]
    # a location the tool has no argument for is still checked, and says how to join
    unseen = call("crew_say", body="hi", git_remote="git@github.com:acme/never-seen-anywhere.git")
    assert unseen.startswith("INVALID ARGUMENTS: crew_say acts on this session's crew"), unseen
    assert 'crew_status(git_remote="git@github.com:acme/never-seen-anywhere.git")' in unseen, unseen


# -- note and intent on a claim ---------------------------------------------------------------


def test_a_claims_note_or_intent_is_its_reason(live, clock):
    project = _seated(live, "note")
    assert call("crew_claim", zone="schema:main", note="rewriting the migration").startswith("GRANTED schema:main")
    assert call("crew_claim", zone="deploy:prod", intent="shipping 1.2").startswith("GRANTED deploy:prod")
    assert [(c["resource"], c["reason"]) for c in _claims(live, project)] == [
        ("schema:main", "rewriting the migration"),
        ("deploy:prod", "shipping 1.2"),
    ]
