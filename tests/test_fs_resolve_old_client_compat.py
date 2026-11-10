"""F5 follow-up (fault survey 2026-11-10): a server upgrade must not fail the clients already installed.

F5 made the server refuse (400) a repository it cannot identify when the caller's configured project
names only folders (``hint_scope=folders``): an unreadable ``git_remote`` or ``root_commit``, or
``git_repo``, and no ``root_path``. Every 0.16.1 to 0.17.5 client sends ``hint_scope=folders``, was
always answered with its configured project there, and (the MCP client) keeps no queue for a refused
close: the handoff would be gone. So the refusal is for callers that ask for it (``strict_project``,
which the MCP client now sends); everyone else gets the 0.17.5 answer plus a warning that says where
the work went.

Requests below are shaped as the 0.17.5 clients build them (``_locator`` in the MCP server,
``Memory.session_brief`` / ``Memory.close_session``), against a live API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.crew import schemas as S
from remembra.mcp.crew import CrewBridge
from remembra.relay.handoff import CHECKPOINT_REASON
from remembra.relay.identity import ProjectLocator
from remembra.security.untrusted import unwrap_untrusted
from tests.crew.mcp_support import LiveServer, live_server

# What the server cannot fingerprint: a remote's name, a bare owner/repo, a host with no path, a relative path.
UNREADABLE_REMOTES = ("origin", "owner/repo", "git@github.com", "../upstream")
# The same for the other two ways a caller names a repository without a path.
UNREADABLE_LOCATIONS: tuple[dict[str, Any], ...] = (
    *({"git_remote": remote, "git_repo": True} for remote in UNREADABLE_REMOTES),
    {"root_commit": "not-a-commit", "git_repo": True},
    {"git_repo": True, "repo_name": "widget"},
)
IDS = [next(str(v) for k, v in loc.items() if k != "git_repo") for loc in UNREADABLE_LOCATIONS]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-old-client")) as s:
        s.state["owner"] = s.user("fs-old-client@example.com")
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


_counter = iter(range(1, 10_000))


def project_name(prefix: str = "configured") -> str:
    return f"oc{prefix}{next(_counter)}"


def headers(live: LiveServer) -> dict[str, str]:
    return {"X-API-Key": live.state["key"]}


def old_locator(location: dict[str, Any], configured: str) -> dict[str, Any]:
    """The locator a 0.17.5 MCP client sends for a location the agent passed (no ``strict_project``)."""
    return {**location, "hint_scope": "folders", "hint_project": configured}


def as_query(locator: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in locator.items()}


def old_close_body(locator: dict[str, Any], session: str, end_reason: str | None = None) -> dict[str, Any]:
    """``Memory.close_session`` 0.17.5: session, facts, agent, the locator under ``project``."""
    body: dict[str, Any] = {
        "session_id": session,
        "facts": {"branch": "main", "notes": "moved the retry loop into the client", "facts_source": "agent-declared"},
        "agent_id": "claude-code",
        "project": locator,
    }
    if end_reason:
        body["end_reason"] = end_reason
    return body


def handoff_of(live: LiveServer, project: str) -> dict[str, Any] | None:
    brief = live.api("GET", "/session/brief", headers=headers(live), params={"project_id": project, "recent_n": 0})
    return brief.get("handoff")


def is_the_warning(text: str, configured: str) -> bool:
    return "not recognised" in text and f"'{configured}'" in text and "root_path" in text


def is_the_refusal(body: dict[str, Any]) -> bool:
    detail = str(body.get("detail"))
    return "not understood" in detail and "git_remote" in detail and "root_path" in detail


# ---------------------------------------------------------------------------
# (a) A 0.17.5 client is answered as 0.17.5 answered it, and told where the work went
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_old_client_resolve_gets_its_configured_project_and_a_warning(live, location):
    configured = project_name()
    out = live.api("POST", "/projects/resolve", headers=headers(live), json=old_locator(location, configured))
    assert (out["project_id"], out["source"]) == (configured, "hint"), out
    # the 0.17.5 answer, field for field, apart from the warning
    assert {k: out[k] for k in ("created", "persisted", "fingerprint", "kind", "bound")} == {
        "created": False,
        "persisted": False,
        "fingerprint": None,
        "kind": None,
        "bound": False,
    }, out
    assert len(out["warnings"]) == 1 and is_the_warning(out["warnings"][0], configured), out


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_old_client_brief_reads_its_configured_project_and_says_so(live, location):
    configured = project_name()
    params = {"recent_n": 0, "inbox_limit": 0, "agent_id": "claude-code", **as_query(old_locator(location, configured))}
    brief = live.api("GET", "/session/brief", headers=headers(live), params=params)
    assert brief["project_id"] == configured, brief
    assert brief["resolution"] is None, brief  # as 0.17.5 answered: nothing was resolved
    assert [w for w in brief["warnings"] if is_the_warning(w, configured)], brief["warnings"]


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_old_client_close_stores_the_handoff_in_its_configured_project_with_a_warning(live, location):
    configured = project_name()
    session = f"sess-{configured}"
    body = old_close_body(old_locator(location, configured), session)
    out = live.api("POST", "/session/close", headers=headers(live), json=body)
    assert out["project_id"] == configured and out["handoff_id"] and out["changed"] is True, out
    assert out["resolution"]["source"] == "hint", out
    assert [w for w in out["resolution"]["warnings"] if is_the_warning(w, configured)], out["resolution"]
    stored = handoff_of(live, configured)
    assert stored is not None and stored["id"] == out["handoff_id"], stored


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_old_client_checkpoint_close_is_stored_too(live, location):
    """A checkpoint is a close with the checkpoint end reason (``remembra-relay close --checkpoint``, a milestone)."""
    configured = project_name()
    body = old_close_body(old_locator(location, configured), f"sess-{configured}", end_reason=CHECKPOINT_REASON)
    out = live.api("POST", "/session/close", headers=headers(live), json=body)
    assert out["project_id"] == configured and out["handoff_id"], out
    assert [w for w in out["resolution"]["warnings"] if is_the_warning(w, configured)], out["resolution"]
    stored = handoff_of(live, configured)
    assert stored is not None and stored["id"] == out["handoff_id"], stored
    # the MCP client's milestone wording goes the same way
    again = live.api("POST", "/session/close", headers=headers(live), json={**body, "end_reason": "checkpoint"})
    assert again["project_id"] == configured and again["handoff_id"], again


def test_old_client_trail_reads_its_configured_project_and_says_so(live):
    configured = project_name()
    params = as_query(old_locator({"git_remote": "origin", "git_repo": True}, configured))
    out = live.api("GET", "/trail", headers=headers(live), params=params)
    assert out["resolution"] is None and out["project_id"] == configured and out["items"] == [], out
    assert [w for w in out["warnings"] if is_the_warning(w, configured)], out
    # a trail that names its project has no such key, as before
    named = live.api("GET", "/trail", headers=headers(live), params={"project_id": configured})
    assert "warnings" not in named, named


def test_nothing_changes_for_a_location_that_names_no_repository_or_is_readable(live):
    configured = project_name()
    # no repository named: the configured project, without a warning
    body: dict[str, Any] = {"hint_project": configured, "hint_scope": "folders"}
    bare = live.api("POST", "/projects/resolve", headers=headers(live), json=body)
    assert (bare["project_id"], bare["source"], bare["warnings"]) == (configured, "hint", []), bare
    # a 0.16.0 client (no hint_scope: one namespace by design) is not warned either
    body = {"git_remote": "origin", "hint_project": configured}
    legacy = live.api("POST", "/projects/resolve", headers=headers(live), json=body)
    assert (legacy["project_id"], legacy["source"], legacy["warnings"]) == (configured, "hint", []), legacy
    # a readable remote gets the repository's own project, with or without the opt-in
    repo = project_name("widget")
    for extra in ({}, {"strict_project": True}):
        body = {**old_locator({"git_remote": f"https://github.com/acme/{repo}", "git_repo": True}, configured), **extra}
        out = live.api("POST", "/projects/resolve", headers=headers(live), json=body)
        assert out["project_id"] == repo and out["warnings"] == [], out


# ---------------------------------------------------------------------------
# (b) A client that asks for it is refused, with what to send instead
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_strict_resolve_is_refused(live, location):
    body = {**old_locator(location, project_name()), "strict_project": True}
    assert is_the_refusal(live.api("POST", "/projects/resolve", headers=headers(live), json=body, status=400))


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
def test_strict_brief_is_refused(live, location):
    params = {"recent_n": 0, **as_query({**old_locator(location, project_name()), "strict_project": True})}
    assert is_the_refusal(live.api("GET", "/session/brief", headers=headers(live), params=params, status=400))


@pytest.mark.parametrize("location", UNREADABLE_LOCATIONS, ids=IDS)
@pytest.mark.parametrize("end_reason", [None, CHECKPOINT_REASON], ids=["close", "checkpoint"])
def test_strict_close_and_checkpoint_are_refused_and_store_nothing(live, location, end_reason):
    configured = project_name()
    locator = {**old_locator(location, configured), "strict_project": True}
    body = old_close_body(locator, f"sess-{configured}", end_reason=end_reason)
    assert is_the_refusal(live.api("POST", "/session/close", headers=headers(live), json=body, status=400))
    assert handoff_of(live, configured) is None


def test_strict_trail_is_refused(live):
    params = as_query({**old_locator({"git_remote": "origin", "git_repo": True}, project_name()), "strict_project": True})
    assert is_the_refusal(live.api("GET", "/trail", headers=headers(live), params=params, status=400))


def test_strict_false_is_the_old_answer(live):
    configured = project_name()
    body = {**old_locator({"git_remote": "origin", "git_repo": True}, configured), "strict_project": False}
    out = live.api("POST", "/projects/resolve", headers=headers(live), json=body)
    assert out["project_id"] == configured and len(out["warnings"]) == 1, out


def test_a_restricted_key_that_may_use_the_hint_is_neither_refused_nor_warned():
    from remembra.services.relay import names_unrecognised_repository  # new with this fix

    locator = ProjectLocator(git_remote="origin")
    assert names_unrecognised_repository(locator, "shared", "folders", True) is True
    assert names_unrecognised_repository(locator, "shared", "folders", True, ["shared"]) is False
    assert names_unrecognised_repository(locator, "shared", "folders", True, ["other"]) is True
    assert names_unrecognised_repository(locator, "shared", "all", True) is False
    assert names_unrecognised_repository(locator, None, "folders", True) is False
    assert names_unrecognised_repository(ProjectLocator(root_path="/srv/app", host="h"), "shared", "folders", True) is False
    assert names_unrecognised_repository(ProjectLocator(), "shared", "folders", None) is False


# ---------------------------------------------------------------------------
# (c) The MCP client of this release asks for the refusal and shows it to the agent
# ---------------------------------------------------------------------------


@pytest.fixture()
def stdio(monkeypatch):
    """A fresh stdio MCP process: no remembered project, no seat."""
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    monkeypatch.delenv("REMEMBRA_RELAY_PROJECT", raising=False)
    yield
    server._client = None


def use(live: LiveServer, session: str, project: str, monkeypatch: Any) -> list[dict[str, Any]]:
    """The client an MCP server builds from its environment; returns the requests it goes on to send."""
    client = Memory(
        base_url=live.url,
        api_key=live.state["key"],
        agent_id="claude-code",
        session_id=session,
        project=project,
        provenance_source="mcp",
    )
    sent: list[dict[str, Any]] = []
    real = client._request

    def recording(method: str, path: str, **kw: Any) -> Any:
        sent.append({"method": method, "path": path, **kw})
        return real(method, path, **kw)

    monkeypatch.setattr(client, "_request", recording)
    server._client = client
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", project)
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "claude-code")
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", session)
    return sent


def call(name: str, **args: Any) -> tuple[dict[str, Any], bool]:
    """``(result JSON, isError)`` of a tool call through the dispatch path an MCP client uses."""
    out = asyncio.run(server.mcp.call_tool(name, args))
    failed = bool(getattr(out, "isError", False))
    blocks = out[0] if isinstance(out, tuple) else getattr(out, "content", out)
    text = str(blocks[0].text)
    try:
        return json.loads(text), failed
    except ValueError:
        body, _, _ = text.rpartition("\n" + S.DATA_CLOSE)
        return json.loads(unwrap_untrusted(body + "\n" + S.DATA_CLOSE)), failed


def test_the_mcp_locator_asks_for_the_refusal(stdio, monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "shared")
    for remote in ("origin", "https://github.com/acme/widget"):
        locator, _ = server._locator(git_remote=remote, root_path=None)
        assert locator is not None and locator["strict_project"] is True, locator


@pytest.mark.parametrize("remote", UNREADABLE_REMOTES)
def test_mcp_session_brief_with_an_unreadable_remote_is_an_error_not_the_configured_brief(live, stdio, monkeypatch, remote):
    configured = project_name()
    sent = use(live, f"sess-{configured}", configured, monkeypatch)
    out, failed = call("session_brief", git_remote=remote)
    assert failed and out["status"] == "error" and out["code"] == 400, out
    assert "not understood" in out["error"] and "root_path" in out["error"], out
    briefs = [r for r in sent if r["path"].endswith("/session/brief")]
    assert briefs and all(r["params"].get("strict_project") is True for r in briefs), briefs
    assert server._session_project().get("project_id") is None  # nothing chosen for the calls that follow


@pytest.mark.parametrize("remote", UNREADABLE_REMOTES)
def test_mcp_close_with_an_unreadable_remote_is_refused_and_the_agent_can_file_it_again(live, stdio, monkeypatch, remote):
    configured, repo = project_name(), project_name("widget")
    sent = use(live, f"sess-{configured}", configured, monkeypatch)
    out, failed = call("close_session", facts={"branch": "main", "notes": "moved the retry loop"}, git_remote=remote)
    # told what to send, flagged as a failure, and nothing landed in the configured project
    assert failed and out["status"] == "error" and out["code"] == 400, out
    assert "not understood" in out["error"] and "root_path" in out["error"], out
    closes = [r for r in sent if r["path"].endswith("/session/close")]
    assert len(closes) == 1 and closes[0]["json"]["project"]["strict_project"] is True, closes
    assert closes[0]["json"]["project"]["hint_project"] == configured, closes
    assert handoff_of(live, configured) is None
    # the same call with a remote the server reads stores the handoff under the repository
    again, failed = call(
        "close_session", facts={"branch": "main", "notes": "moved the retry loop"}, git_remote=f"https://github.com/acme/{repo}"
    )
    assert not failed and again["status"] == "ok" and again["project_id"] == repo, again
    assert handoff_of(live, repo) is not None and handoff_of(live, configured) is None
