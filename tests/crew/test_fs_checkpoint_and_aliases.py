"""F39: guessed argument names and a checkpoint without files no longer fail validation.

Every call goes through ``FastMCP.call_tool`` (the dispatch path an MCP client uses) into the real MCP
server module and a live API (``tests/crew/mcp_support.py``). One test also goes over an in-memory MCP
client session, so the advertised ``outputSchema`` is enforced on the answers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


from mcp.server.fastmcp.exceptions import ToolError

import remembra.mcp.server as server
from remembra.crew import schemas as S
import tests.crew.test_mcp_crew as base
from tests.crew.test_mcp_crew import (
    _two_agents,
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


def _schemas() -> dict[str, dict[str, Any]]:
    return {t.name: t.inputSchema for t in asyncio.run(server.mcp.list_tools())}


# -- files_changed is optional ---------------------------------------------------------------


def test_a_checkpoint_without_files_is_recorded_and_says_no_files_were_declared(live, clock):
    project = _seated(live, "ckpnofiles")
    out = call("crew_checkpoint", summary="deployed to production")
    assert out.startswith("CHECKPOINT ckp_"), out
    assert "recorded" in out.splitlines()[0], out
    assert "No files declared" in out, out
    rows = live.crew_rows("SELECT trigger, facts FROM crew_checkpoints WHERE crew_id = ?", (crew_id_of(live, project),))
    assert len(rows) == 1
    facts = json.loads(rows[0]["facts"])
    assert facts["files_changed"] == [] and facts["notes"] == "deployed to production"
    # with files the note is absent
    with_files = call("crew_checkpoint", files_changed=["src/a.py"], summary="edited a")
    assert with_files.startswith("CHECKPOINT ckp_") and "No files declared" not in with_files, with_files


def test_an_explicit_empty_or_null_file_list_is_the_same_as_none(live, clock):
    _seated(live, "ckpempty")
    assert call("crew_checkpoint", files_changed=[], summary="tests only").startswith("CHECKPOINT ckp_")
    assert call("crew_checkpoint", files_changed=None, summary="docs read").startswith("CHECKPOINT ckp_")


def test_the_contract_and_the_schema_no_longer_require_files_changed():
    assert S.validate_mcp_call("crew_checkpoint", {"summary": "x"}) == []
    assert S.validate_mcp_call("crew_checkpoint", {"files_changed": "a.py"}) == ["files_changed: expected list[str]"]
    assert "files_changed" not in (_schemas()["crew_checkpoint"].get("required") or [])


# -- guessed names ---------------------------------------------------------------------------


def test_crew_say_takes_message_for_body(live, clock):
    project = _seated(live, "saymsg")
    out = call("crew_say", message="the build is green", to="crew")
    assert out.startswith("SENT msg_"), out
    rows = live.crew_rows("SELECT body FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),))
    assert [r["body"] for r in rows] == ["the build is green"]
    # the same text under both names is one message; two different texts are refused, nothing sent
    assert call("crew_say", body="again", message="again").startswith("SENT msg_")
    both = call("crew_say", body="one", message="two")
    assert both.startswith("INVALID ARGUMENTS:") and "body" in both and "message" in both, both
    rows = live.crew_rows("SELECT body FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),))
    assert sorted(r["body"] for r in rows) == ["again", "the build is green"]


def test_send_to_inbox_takes_to_for_to_agent(live, clock):
    _seated(live, "inboxto")
    out = json.loads(call("send_to_inbox", to="codex", subject="handover", body="please review"))
    assert out["ok"] is True and out["to_agent"] == "codex", out
    items = as_json(call("get_inbox", agent_id="codex"))["items"]
    assert [i["subject"] for i in items if i["inbox_id"] == out["inbox_id"]] == ["handover"]


def test_ack_inbox_takes_inbox_ids_and_acknowledges_each(live, clock):
    _seated(live, "ackids")
    ids = [json.loads(call("send_to_inbox", to_agent="gemini", subject=f"s{i}", body="b"))["inbox_id"] for i in range(3)]
    one = json.loads(call("ack_inbox", inbox_ids=[ids[0]], result="done"))
    assert one["ok"] is True and one["inbox_id"] == ids[0] and one["ack_result"] == "done", one
    many = json.loads(call("ack_inbox", inbox_ids=ids[1:], result="done", note="handled"))
    assert many["ok"] is True and many["count"] == 2, many
    assert [i["inbox_id"] for i in many["items"]] == ids[1:]
    assert all(i["ack_result"] == "done" and i["ack_note"] == "handled" for i in many["items"])
    unread = {i["inbox_id"] for i in as_json(call("get_inbox", agent_id="gemini"))["items"]}
    assert unread.isdisjoint(ids)
    # the single-id form is unchanged, and no id at all is one clear line
    again = json.loads(call("ack_inbox", inbox_id=ids[0]))
    assert again["ok"] is True and again["inbox_id"] == ids[0]
    for empty in ({}, {"inbox_ids": []}, {"inbox_id": None}):
        none = call("ack_inbox", **empty)
        assert none.startswith("INVALID ARGUMENTS:") and "inbox_id" in none, none


def test_remembra_help_takes_topic_or_query_for_question(clock):
    asked = json.loads(call("remembra_help", question="how do I release a claim"))
    assert json.loads(call("remembra_help", topic="how do I release a claim")) == asked
    assert json.loads(call("remembra_help", query="how do I release a claim")) == asked


def test_the_advertised_schema_carries_the_aliases_and_does_not_require_the_canonical_name():
    schemas = _schemas()
    for tool, canonical, alias in (
        ("crew_say", "body", "message"),
        ("send_to_inbox", "to_agent", "to"),
        ("remembra_help", "question", "topic"),
        ("ack_inbox", "inbox_id", "inbox_ids"),
    ):
        schema = schemas[tool]
        assert alias in schema["properties"], (tool, alias)
        assert canonical in schema["properties"], (tool, canonical)
        assert canonical not in (schema.get("required") or []), (tool, canonical)
    assert schemas["send_to_inbox"]["required"] == ["subject", "body"]


# -- one error format ------------------------------------------------------------------------


class _Untouched:
    """A client no refused call may reach."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the client was used ({name}) by a call that should have been refused")


def test_a_missing_required_argument_is_one_plain_line_for_every_tool(clock, monkeypatch):
    monkeypatch.setattr(server, "_client", _Untouched())
    seen = []
    for name, schema in _schemas().items():
        if name in ("crew_say", "send_to_inbox", "remembra_help", "ack_inbox"):
            required = {"crew_say": "body", "send_to_inbox": "to_agent", "remembra_help": "question", "ack_inbox": "inbox_id"}[
                name
            ]
        elif schema.get("required"):
            required = schema["required"][0]
        else:
            continue
        out = call(name)
        assert out.startswith("INVALID ARGUMENTS:"), (name, out)
        assert f"{required}: required" in out, (name, out)
        assert f"{name} takes:" in out, (name, out)
        for leak in ("pydantic", "input_value", "validation error", "type=missing"):
            assert leak not in out, (name, leak, out)
        seen.append(name)
    assert {"crew_say", "crew_guard", "crew_report", "store_memory", "update_memory", "ack_inbox"} <= set(seen)


def test_a_wrong_type_names_the_argument_without_echoing_the_value(live, clock):
    _seated(live, "badtype")
    # commits sent as {sha, subject} are read since F16 (tests/crew/test_fs_close_facts_shapes.py): the wrong type
    # shown here is one no reading covers, a mapping where the list of paths goes
    out = call("crew_checkpoint", summary="x", files_changed={"path": "SECRETSUBJECT"})
    assert out.startswith("INVALID ARGUMENTS:") and "files_changed" in out, out
    assert "SECRETSUBJECT" not in out and "pydantic" not in out and "input_value" not in out, out
    assert "You sent: files_changed, summary." in out, out


def test_answers_and_refusals_over_a_real_mcp_session(live, clock):
    from mcp.shared.memory import create_connected_server_and_client_session

    _seated(live, "wire")

    async def run() -> list[tuple[bool, str]]:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            out = []
            for name, args in (
                ("crew_say", {"to": "crew"}),
                ("crew_say", {"message": "over the wire"}),
                ("crew_checkpoint", {"summary": "nothing changed"}),
                ("crew_checkpoint", {"files_changed": {"path": "a.py"}}),
            ):
                res = await session.call_tool(name, args)
                out.append((bool(res.isError), str(res.content[0].text)))
            return out

    missing, sent, checkpoint, badtype = asyncio.run(run())
    # an argument failure is an MCP error carrying the one line; an answer is not an error
    assert missing == (True, missing[1]) and missing[1].startswith("INVALID ARGUMENTS: body: required"), missing
    assert sent[0] is False and sent[1].startswith("SENT msg_"), sent
    assert checkpoint[0] is False and checkpoint[1].startswith("CHECKPOINT ckp_"), checkpoint
    assert badtype[0] is True and badtype[1].startswith("INVALID ARGUMENTS:"), badtype


# -- the product's own hint ------------------------------------------------------------------


def test_the_release_request_hint_is_a_call_that_validates(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "hint")
    use(a)
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")
    use(b)
    refused = call("crew_claim", zone="pos")
    assert refused.startswith(f"REFUSED: zone pos held EXCLUSIVELY by {a_sign}"), refused
    assert f'crew_say(body="…", to="@{a_sign}", kind="request_release")' in refused, refused
    assert S.validate_mcp_call("crew_say", {"body": "…", "to": f"@{a_sign}", "kind": "request_release"}) == []
    # the documented texts carry the body too
    assert 'crew_say(body="…", to="@holder", kind="request_release")' in (server.crew_claim.__doc__ or "")
    claim = next(t for t in S.MCP_TOOLS if t.name == "crew_claim")
    assert "crew_say(body=…, to=…, kind=request_release)" in claim.returns
    # and the server's own refusal (what hooks and the CLI read) does as well
    crew = crew_id_of(live, project)
    seat = server._crew.seat(server._crew_caller())[0]
    zone = live.crew_rows("SELECT id FROM crew_zones WHERE crew_id = ? AND slug = 'pos'", (crew,))[0]["id"]
    res = live.api(
        "POST",
        f"/crews/{crew}/claims",
        headers={"X-API-Key": live.state["key"], "X-Remembra-Crew-Session": seat.token},
        json={"zone_id": zone, "mode": "exclusive", "wait": False, "source": "mcp"},
        status=409,
    )
    assert f'crew_say(body="…", to="@{a_sign}", kind="request_release")' == res["detail"]["how_to_request"], res
