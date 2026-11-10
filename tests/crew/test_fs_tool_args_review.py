"""F22 / F25 / F39, second pass: what an independent review of the first fix found.

* F25: ``remembra-crew claim`` keeps sending a schema:/deploy:/service:/supabase:/mcp: target as a
  resource even when it is malformed (the server refuses it; it must never become a path glob), and
  a refused claim prints the server's reasons and the form a resource takes.
* F39: an argument failure is an MCP error again (``isError``), in the one-line format; a failure
  raised inside a tool is not reported as an argument failure; an empty canonical value next to an
  alias is not a conflict.
* F22: a location that is not on record is not turned into a project name to switch to.

MCP calls go through ``FastMCP.call_tool`` (or an in-memory MCP client session) into the real MCP
server module and a live API; the CLI's request body is posted to that same API.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from remembra.mcp import arguments as A
from remembra.mcp.crew import CrewApiError
from remembra.relay.crew import cli
from tests.crew.test_mcp_crew import agent, crew_id_of, project_name, use

# the live API and the fresh crew bridge, shared with the WP-11 suite
live = base.live
clock = base.clock

OLD_PATTERN = r"(?:schema|deploy|service|supabase|mcp):[A-Za-z0-9._/-]{1,64}"


def call(name: str, **args: Any) -> str:
    """A tool's answer as text, error or not (``session`` below tells the two apart)."""
    try:
        return base.call(name, **args)
    except ToolError as e:
        return str(e)


def _seated(live: Any, prefix: str, agent_id: str = "claude-desktop") -> str:
    project = project_name(prefix)
    use(agent(live, agent_id, f"{project}-s", project))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")
    return project


def session(*calls: tuple[str, dict[str, Any]]) -> list[tuple[bool, str]]:
    """``(isError, text)`` of each call over a real in-memory MCP client session."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def run() -> list[tuple[bool, str]]:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            out = []
            for name, args in calls:
                res = await client.call_tool(name, args)
                out.append((bool(res.isError), str(res.content[0].text)))
            return out

    return asyncio.run(run())


# -- F25: the CLI ------------------------------------------------------------------------------


def _cli_claim(live: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str) -> tuple[int, str]:
    """Run ``remembra-crew claim <target>`` with crewd's API relay replaced by the live test API."""
    caller = server._crew_caller()
    seat, _ = server._crew.seat(caller)
    http = server._crew._http(caller, seat)

    def api(layout: Any, method: str, path: str, *, json_body: Any = None, **_: Any) -> dict[str, Any]:
        try:
            status, body, _etag = http.call(method, path, token=seat.token, json=json_body)
        except CrewApiError as e:
            return {"status": e.status, "body": {"detail": e.body}}
        return {"status": status, "body": body}

    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_need_session", lambda layout: {"crew_id": seat.crew_id, "session_id": seat.session_id})
    monkeypatch.setattr(cli, "_snapshot", lambda layout, crew_id: {"zones": []})
    args = argparse.Namespace(target=target, mode="exclusive", wait=0, task=None, reason=None)
    capsys.readouterr()
    code = cli.cmd_claim(args, None)  # type: ignore[arg-type]
    # the live API logs to stdout too: the command's own answer is its last line
    return code, capsys.readouterr().out.strip().splitlines()[-1]


@pytest.mark.parametrize("target", ["deploy:prod@1", "service:", "deploy:prod env", "mcp:" + "x" * 70, "schema:"])
def test_the_cli_never_turns_a_malformed_classic_resource_into_a_path_glob(target, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli._claim_target({"zones": []}, target) == {"resource": target}


def test_a_malformed_classic_resource_is_refused_by_the_server_not_granted_as_a_path(live, clock, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    project = _seated(live, "cliclassic")
    code, out = _cli_claim(live, monkeypatch, capsys, "deploy:prod@1")
    assert code == 1 and out.startswith("REFUSED: The body is invalid."), out
    assert "GRANTED" not in out, out
    assert live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ?", (crew_id_of(live, project),)) == []
    # the refusal says which field and what a resource looks like, without repeating the value
    assert "resource: does not match" in out and "kind:name" in out and "remembra-crew claim artifact:qa-report" in out, out
    assert "prod@1" not in out, out
    # a well-formed one is granted as a resource
    code, out = _cli_claim(live, monkeypatch, capsys, "deploy:prod")
    assert code == 0 and out.startswith("GRANTED deploy:prod"), out
    rows = live.crew_rows("SELECT resource, path_glob FROM crew_claims WHERE crew_id = ?", (crew_id_of(live, project),))
    assert [(r["resource"], r["path_glob"]) for r in rows] == [("deploy:prod", None)]


def test_the_cli_refusal_from_an_older_server_names_the_kinds_it_takes(tmp_path, monkeypatch, capsys):
    """A 0.17.5 server answers a free kind with 422 and its five-kind pattern in ``problems``."""
    monkeypatch.chdir(tmp_path)
    sent: list[Any] = []

    def api(layout: Any, method: str, path: str, *, json_body: Any = None, **_: Any) -> dict[str, Any]:
        sent.append(json_body)
        detail = {
            "error": "invalid_body",
            "message": "The body is invalid.",
            "problems": [f"$.resource: 'artifact:qa' does not match {OLD_PATTERN}"],
        }
        return {"status": 422, "body": {"detail": detail}}

    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_need_session", lambda layout: {"crew_id": "crw_1", "session_id": "cs_1"})
    monkeypatch.setattr(cli, "_snapshot", lambda layout, crew_id: {"zones": []})
    args = argparse.Namespace(target="artifact:qa", mode="exclusive", wait=0, task=None, reason=None)
    assert cli.cmd_claim(args, None) == 1  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert sent[0]["resource"] == "artifact:qa"
    assert out.startswith("REFUSED: The body is invalid."), out
    assert "schema:, deploy:, service:, supabase: and mcp:" in out and "remembra-crew claim service:<name>" in out, out
    assert "artifact:qa" not in out.replace("remembra-crew claim artifact:qa-report", ""), out
    # a failure without problems prints as before
    monkeypatch.setattr(cli, "_api", lambda *a, **k: {"status": 500, "body": {"detail": {"message": "boom"}}})
    assert cli.cmd_claim(args, None) == 1  # type: ignore[arg-type]
    assert capsys.readouterr().out == "REFUSED: boom\n"


# -- F39: an argument failure is an error, in one line -------------------------------------------


def test_argument_failures_are_mcp_errors_and_real_answers_are_not(live, clock):
    project = _seated(live, "iserror")
    missing, unknown, badtype, noid, wrong_crew, sent = session(
        ("crew_say", {}),
        ("crew_claim", {"zone": "schema:x", "bogus": 1}),
        ("crew_say", {"body": 5}),
        ("ack_inbox", {}),
        # a project named on a crew call is where it acts since F6; a remote the tool has no argument for is checked
        ("crew_say", {"body": "x", "git_remote": "git@github.com:acme/iserror-never-seen.git"}),
        ("crew_say", {"message": "over the wire"}),
    )
    assert missing == (True, missing[1]) and missing[1].startswith("INVALID ARGUMENTS: body: required. crew_say takes:"), missing
    assert unknown[0] is True and unknown[1].startswith("INVALID ARGUMENTS: crew_claim does not take bogus."), unknown
    assert badtype[0] is True and badtype[1].startswith("INVALID ARGUMENTS: body: "), badtype
    assert noid[0] is True and noid[1].startswith("INVALID ARGUMENTS: inbox_id: required. ack_inbox takes:"), noid
    assert wrong_crew[0] is True, wrong_crew
    assert wrong_crew[1].startswith("INVALID ARGUMENTS: crew_say acts on this session's crew"), wrong_crew
    for _, text in (missing, unknown, badtype, noid, wrong_crew):
        assert "\n" not in text and "Error executing tool" not in text and "pydantic" not in text, text
    assert sent[0] is False and sent[1].startswith("SENT msg_"), sent
    rows = live.crew_rows("SELECT body FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),))
    assert [r["body"] for r in rows] == ["over the wire"]


def test_a_validation_error_raised_inside_a_tool_is_not_an_argument_failure(monkeypatch):
    class Inner(BaseModel):
        n: int

    def broken(verbose: bool = False) -> str:
        Inner.model_validate({"n": "not a number"})
        return "unreachable"

    tool = server.mcp._tool_manager.get_tool("health_check")
    assert tool is not None
    monkeypatch.setattr(tool, "fn", broken)
    with pytest.raises(ToolError) as err:
        base.call("health_check")
    text = str(err.value)
    assert text.startswith("Error executing tool health_check:") and "INVALID ARGUMENTS" not in text, text


def test_an_empty_canonical_value_next_to_an_alias_is_not_a_conflict(live, clock):
    project = _seated(live, "emptycanon")
    assert call("crew_say", body="", message="hello").startswith("SENT msg_")
    assert call("crew_say", body="   ", message="again").startswith("SENT msg_")
    rows = live.crew_rows("SELECT body FROM crew_messages WHERE crew_id = ? AND kind = 'chat'", (crew_id_of(live, project),))
    assert sorted(r["body"] for r in rows) == ["again", "hello"]
    # two real values that differ are still refused, and an empty alias changes nothing
    assert call("crew_say", body="one", message="two").startswith("INVALID ARGUMENTS: body and message are the same")
    kept = A.prepare("crew_say", ("body", "kind", "to"), {"body": "kept", "message": ""})
    assert kept.refusal is None and kept.arguments == {"body": "kept"} and kept.sent_as == {}


def test_every_refusal_ends_its_reasons_before_listing_the_arguments(live, clock):
    _seated(live, "sentence")
    both = call("crew_say", body="one", message="two")
    assert "differ: pass one. crew_say takes: body, kind, to, thread, wait_s, project_id, root_path. Nothing was done." in both
    help_ = call("remembra_help", topic="claims", query="other")
    assert "pass one. remembra_help takes:" in help_, help_
    # a blank project is no project on a crew tool since F6; the blank that is refused is a location it has no argument for
    blank = call("crew_claim", zone="deploy:prod", git_remote="  ")
    assert "expected a non-empty string. crew_claim takes:" in blank, blank
    # a reason that already ends its sentence is left alone
    hinted = call("crew_checkpoint", summary="x", trigger="deploy")
    assert ".. " not in hinted and "Say what happened in summary. crew_checkpoint takes:" in hinted, hinted


# -- F22: a location that is not on record -------------------------------------------------------


def test_an_unknown_location_is_not_turned_into_a_project_to_switch_to(live, clock, tmp_path):
    project = _seated(live, "noinvent")
    # root_path is an argument of the crew tools since F5 (it names the call's project), so the location that
    # is checked against the crew is the one they have no argument for: a remote
    out = call("crew_checkpoint", summary="x", git_remote="git@github.com:acme/zzz-never-a-project.git")
    assert out.startswith("INVALID ARGUMENTS: crew_checkpoint acts on this session's crew"), out
    assert f"which is {project}" in out and "Nothing was done." in out, out
    assert "; you named" not in out and "crew_status(project_id=" not in out, out
    assert "is not on record for any project" in out and 'crew_status(git_remote="' in out, out
    assert live.crew_rows("SELECT id FROM crew_checkpoints WHERE crew_id = ?", (crew_id_of(live, project),)) == []
    unseen = call("crew_claim", zone="schema:other", git_remote="git@github.com:acme/never-seen-anywhere.git")
    assert "; you named" not in unseen and "is not on record for any project" in unseen, unseen
    assert 'crew_status(git_remote="git@github.com:acme/never-seen-anywhere.git")' in unseen, unseen


def test_a_location_on_record_for_another_project_still_names_it(live, clock):
    project, other = project_name("onrecord"), project_name("onrecordother")
    for name in (project, other):
        live.api(
            "POST",
            "/projects/resolve",
            headers={"X-API-Key": live.state["key"]},
            json={"git_remote": f"https://github.com/acme/{name}.git", "hint_project": name},
        )
    use(agent(live, "codex", f"{project}-s", "default"))
    assert call("crew_status", git_remote=f"git@github.com:acme/{project}.git").startswith(f"CREW {project} ")
    out = call("crew_claim", zone="schema:other", git_remote=f"git@github.com:acme/{other}.git")
    assert f"; you named {other}" in out and f'crew_status(project_id="{other}")' in out, out
