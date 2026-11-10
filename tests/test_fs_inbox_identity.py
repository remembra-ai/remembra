"""The inbox tools say which agent this connection is when the answer depends on it (F8).

Found live on 0.17.5: a Claude Code session started from the Claude desktop app gets its Remembra tools from the
desktop app's server (agent ``claude-desktop``, its own agent-scoped key), while the crew hooks of the same
session run as ``claude-code`` and announce "MESSAGE inbox_… for you from codex". ``get_inbox(agent_id=
"claude-code")`` then answered a bare ``Request failed: Inbox not found`` (the server's deliberate answer to an
agent-scoped key asking for another agent's inbox, the same as a missing row), ``ack_inbox`` of the announced id
answered ``Inbox item '…' not found``, replies went out as ``claude-desktop``, and ``health_check`` reported no
warnings.

These tests run the real API (uvicorn, authentication on, real agent-scoped keys) and call the MCP tools as the
stdio server does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.relay.adapters.crew_hooks import CrewCommands, crew_specs, render_json_hooks
from remembra.relay.crew.verify import adapters_path, render_adapters
from remembra.security.untrusted import unwrap_untrusted
from tests.crew.mcp_support import LiveServer, live_server


def _j(raw: str) -> dict[str, Any]:
    try:
        return dict(json.loads(raw))
    except ValueError:
        return dict(json.loads(unwrap_untrusted(raw)))


@pytest.fixture()
def live(tmp_path) -> Any:
    with live_server(tmp_path, crew=False) as srv:
        owner = srv.user("owner@example.com")
        srv.state["keys"] = {
            "claude-desktop": srv.key(owner, "editor", agent_id="claude-desktop"),
            "claude-code": srv.key(owner, "editor", agent_id="claude-code"),
            "codex": srv.key(owner, "editor", agent_id="codex"),
            "admin": srv.key(owner, "admin"),
        }
        yield srv


def _as(monkeypatch: pytest.MonkeyPatch, live: LiveServer, agent: str, *, key: str | None = None) -> None:
    """Configure the stdio MCP server process as ``agent`` (its env), with that agent's key unless ``key`` names another."""
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_URL", live.url)
    monkeypatch.setattr(server, "REMEMBRA_API_KEY", live.state["keys"][key or agent])
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", agent)
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "alpha")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", f"sess-{agent}")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})


def _hooks_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str = "claude-code") -> Path:
    """A home directory where ``adapter``'s crew hooks are installed the way ``remembra-crew connect`` writes them."""
    home = tmp_path / "home"
    config = home / ".claude" / "settings.json"
    config.parent.mkdir(parents=True)
    cmds = CrewCommands(python="/usr/bin/python3", gate=str(home / ".remembra/crew/bin/crew-gate.py"), crew="remembra-crew")
    text, _ = render_json_hooks('{"permissions": {"allow": []}}', crew_specs()[adapter], cmds)
    config.write_text(text)
    status = adapters_path(home)
    status.parent.mkdir(parents=True)
    status.write_text(render_adapters(None, {adapter: {"installed": True, "config": [str(config)]}}))
    monkeypatch.setenv("HOME", str(home))
    return home


def _codex_sends(live: LiveServer, to: str, subject: str) -> str:
    sent = live.api(
        "POST",
        "/inbox/send",
        headers={"X-API-Key": live.state["keys"]["codex"], "X-Remembra-Agent-Id": "codex"},
        status=(200, 201),
        json={"to_agent": to, "from_agent": "codex", "subject": subject, "body": "please look", "metadata": {}},
    )
    return str(sent["inbox_id"])


def test_get_inbox_for_another_agent_id_names_both_identities(live, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "no-hooks"))
    _codex_sends(live, "claude-code", "for the hook identity")
    _as(monkeypatch, live, "claude-desktop")

    out = _j(server.get_inbox(agent_id="claude-code", limit=3))

    assert out["ok"] is False and out["code"] == 404 and out["error"] == "identity_mismatch", out
    assert out["this_agent"] == "claude-desktop" and out["requested"] == "claude-code", out
    assert "signed in as 'claude-desktop'" in out["message"] and "'claude-code'" in out["message"], out
    assert "Inbox not found" in out["detail"], out
    # its own inbox is untouched by this: the default call still answers, and so does asking for itself by name
    assert _j(server.get_inbox())["agent_id"] == "claude-desktop"
    assert _j(server.get_inbox(agent_id="claude-desktop"))["ok"] is True


def test_get_inbox_says_when_the_other_id_is_the_one_this_machines_hooks_run_as(live, monkeypatch, tmp_path):
    _hooks_home(tmp_path, monkeypatch)
    _as(monkeypatch, live, "claude-desktop")
    out = _j(server.get_inbox(agent_id="claude-code"))
    assert out["error"] == "identity_mismatch", out
    assert "crew hooks on this machine run as 'claude-code'" in out["message"], out
    assert "health_check" in out["message"], out


def test_ack_of_a_message_addressed_to_another_agent_id_names_this_identity(live, monkeypatch, tmp_path):
    _hooks_home(tmp_path, monkeypatch)
    theirs = _codex_sends(live, "claude-code", "announced by the hook")
    mine = _codex_sends(live, "claude-desktop", "really mine")
    _as(monkeypatch, live, "claude-desktop")

    out = _j(server.ack_inbox(theirs, result="done"))
    assert out["ok"] is False and out["code"] == 404 and "not found" in out["error"], out
    assert out["this_agent"] == "claude-desktop", out
    assert "signed in as 'claude-desktop'" in out["error"] and "'claude-code'" in out["error"], out
    # the message is still unread for the agent it was sent to, and this agent's own ack works
    _as(monkeypatch, live, "claude-code")
    assert [i["inbox_id"] for i in _j(server.get_inbox())["items"]] == [theirs]
    _as(monkeypatch, live, "claude-desktop")
    assert _j(server.ack_inbox(mine, result="done"))["ok"] is True


def test_health_check_and_send_warn_when_the_hooks_run_as_another_agent_id(live, monkeypatch, tmp_path):
    _hooks_home(tmp_path, monkeypatch)
    _as(monkeypatch, live, "claude-desktop")

    health = _j(server.health_check())
    split = [w for w in health["warnings"] if "crew hooks" in w]
    assert health["agent_id"] == "claude-desktop" and len(split) == 1, health
    assert "'claude-code'" in split[0] and "'claude-desktop'" in split[0] and "REMEMBRA_AGENT_ID" in split[0], split

    sent = _j(server.send_to_inbox("codex", "status", "claude-code here."))
    assert sent["ok"] is True and sent["from_agent"] == "claude-desktop", sent
    assert any("sent as 'claude-desktop'" in w and "'claude-code'" in w for w in sent["warnings"]), sent

    own = _j(server.get_inbox())
    assert own["ok"] is True and any("'claude-code'" in w for w in own["warnings"]), own


def test_one_identity_or_no_hooks_or_a_remote_server_gets_no_identity_warning(live, monkeypatch, tmp_path):
    # the hooks and this server are the same agent id: the normal terminal-launched Claude Code session
    _hooks_home(tmp_path, monkeypatch)
    _as(monkeypatch, live, "claude-code")
    assert not [w for w in _j(server.health_check())["warnings"] if "crew hooks" in w]
    sent = _j(server.send_to_inbox("codex", "status", "hello"))
    assert sent["ok"] is True and sent["warnings"] == [], sent
    assert "warnings" not in _j(server.get_inbox())

    # another program's hooks say nothing about this agent (Codex beside Claude Code)
    _as(monkeypatch, live, "codex")
    assert not [w for w in _j(server.health_check())["warnings"] if "crew hooks" in w]

    # a remote transport never reads the server's own home directory for a caller
    _as(monkeypatch, live, "claude-desktop")
    assert server._hook_agent_ids("claude-desktop") == ["claude-code"]
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    assert server._hook_agent_ids("claude-desktop") == []

    # no hooks on the machine, an unreadable status file, no agent id
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    assert server._hook_agent_ids("claude-desktop") == []
    assert server._hook_agent_ids("") == []
    broken = tmp_path / "broken"
    adapters_path(broken).parent.mkdir(parents=True)
    adapters_path(broken).write_text("{not json")
    monkeypatch.setenv("HOME", str(broken))
    assert server._hook_agent_ids("claude-desktop") == []
    assert not [w for w in _j(server.health_check())["warnings"] if "crew hooks" in w]


def test_a_key_that_may_read_every_inbox_and_a_plain_missing_row_are_unchanged(live, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "no-hooks"))
    _codex_sends(live, "claude-code", "readable with the owner's key")
    _as(monkeypatch, live, "claude-desktop", key="admin")
    out = _j(server.get_inbox(agent_id="claude-code"))
    assert out["ok"] is True and out["count"] == 1, out

    _as(monkeypatch, live, "claude-desktop")
    missing = _j(server.ack_inbox("inbox_0000000000000000"))
    assert missing["ok"] is False and missing["code"] == 404 and "not found" in missing["error"], missing
    assert "crew hooks" not in missing["error"], missing  # no hooks here: only who this connection is
