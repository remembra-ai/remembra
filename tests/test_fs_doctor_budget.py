"""The doctor says which keys it did not check (F54 follow-up).

The doctor sends at most four GETs, so with more than two distinct keys on a
machine some are never asked about. They were dropped silently: ``unchecked``
stayed empty and an agent whose own key was skipped was still told "The key
works". And the key the calling MCP server itself uses (stripped from the
hooks' environment) was never mentioned at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from remembra.marshal import doctor, signals, tools
from remembra.marshal.render import Style
from tests.marshal_fixtures import NOW, URL, FakeHome, FakeTrail, entry

KEY_CLAUDE = "rem_AAAAclaude0000000000000000000000aaaa"
KEY_CODEX = "rem_BBBBcodex00000000000000000000000bbbb"
KEY_CURSOR = "rem_CCCCcursor0000000000000000000000cccc"
KEY_GEMINI = "rem_DDDDgemini0000000000000000000000dddd"
KEY_MCP = "rem_EEEEdesktop000000000000000000000eeee"
ALL_KEYS = (KEY_CLAUDE, KEY_CODEX, KEY_CURSOR, KEY_GEMINI, KEY_MCP)


def _mcp_json(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {"REMEMBRA_URL": URL, "REMEMBRA_API_KEY": key, "REMEMBRA_PROJECT": "default"}
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing["mcpServers"] = {"remembra": {"command": "remembra-mcp", "env": env}}
    path.write_text(json.dumps(existing, indent=2))


@pytest.fixture()
def four_keys(tmp_path: Path) -> FakeHome:
    """Claude Code, Codex, Cursor and Gemini CLI, each with its own key (as on the owner's machine)."""
    fh = FakeHome(tmp_path)
    fh.install("claude", "codex", "cursor-agent", "gemini")
    fh.claude_mcp(key=KEY_CLAUDE)
    fh.codex_mcp(key=KEY_CODEX)
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    fh.hooks("gemini")
    _mcp_json(fh.home / ".gemini" / "settings.json", KEY_GEMINI)
    _mcp_json(fh.home / ".cursor" / "mcp.json", KEY_CURSOR)
    return fh


def _trail() -> FakeTrail:
    return FakeTrail(
        agents={
            "claude-code": {"handoffs": 3, "last_active": signals_iso(NOW - 3600), "daily": [0, 0, 0, 0, 0, 1, 2]},
            "codex": {"handoffs": 2, "last_active": signals_iso(NOW - 7200), "daily": [0, 0, 0, 0, 0, 1, 1]},
        },
        items=[entry("claude-code", "handoff", NOW - 3600), entry("codex", "handoff", NOW - 7200)],
    )


def signals_iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat()


def _run(fh: FakeHome, trail: FakeTrail, agents: list[str] | None = None) -> doctor.Report:
    env = fh.environ()
    return doctor.run(fh.home, agents, True, environ=env, transport=trail.transport, which=fh.which, now=NOW, cwd=fh.root)


def _sent(trail: FakeTrail) -> list[str]:
    return list(dict.fromkeys(r.headers["x-api-key"] for r in trail.requests))


def test_keys_skipped_for_the_read_budget_are_listed_as_not_checked(four_keys: FakeHome) -> None:
    trail = _trail()
    report = _run(four_keys, trail)
    assert len(trail.requests) <= signals.MAX_GETS
    assert _sent(trail) == [KEY_CLAUDE, KEY_CODEX]  # two of the four keys were asked about
    unchecked = report.signals.unchecked
    assert any("~/.cursor/mcp.json" in u and "cursor" in u for u in unchecked), unchecked
    assert any("~/.gemini/settings.json" in u and "gemini" in u for u in unchecked), unchecked
    text = report.text(Style())
    assert "not checked:" in text and "~/.gemini/settings.json" in text
    data = report.json()
    assert any("~/.gemini/settings.json" in u for u in data["unchecked"])
    for key in ALL_KEYS:
        assert key not in text and key not in json.dumps(data, default=str)


def test_an_agent_whose_own_key_was_skipped_is_not_told_the_key_works(four_keys: FakeHome) -> None:
    report = _run(four_keys, _trail())
    gemini = [f for f in report.findings if f.agent == "gemini" and f.id == "HOOKS_NOT_FIRING"]
    assert len(gemini) == 1, [(f.id, f.agent, f.what) for f in report.findings]
    assert "The key works" not in gemini[0].what
    assert "was not checked" in gemini[0].what and "~/.gemini/settings.json" in gemini[0].what
    assert "The key works" not in report.text(Style())


def test_naming_the_agent_checks_its_key_first(four_keys: FakeHome) -> None:
    trail = _trail()
    report = _run(four_keys, trail, ["gemini"])
    assert KEY_GEMINI in _sent(trail)  # the note's command really does check it
    assert not any("~/.gemini/settings.json" in u for u in report.signals.unchecked)
    gemini = [f for f in report.findings if f.agent == "gemini" and f.id == "HOOKS_NOT_FIRING"]
    assert len(gemini) == 1 and gemini[0].what.startswith("The key works")  # now it was asked: the sentence is earned


def test_a_rejected_key_named_with_agent_is_found(four_keys: FakeHome) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-api-key"] == KEY_GEMINI:
            return httpx.Response(401, json={"detail": "Invalid or revoked API key."})
        return _trail().handler(request)

    report = doctor.run(
        four_keys.home,
        ["gemini"],
        True,
        environ=four_keys.environ(),
        transport=httpx.MockTransport(handler),
        which=four_keys.which,
        now=NOW,
        cwd=four_keys.root,
    )
    rejected = [f for f in report.findings if f.id == "KEY_REJECTED"]
    assert len(rejected) == 1 and any("~/.gemini/settings.json" in e for e in rejected[0].evidence)


def test_two_keys_fit_the_budget_and_nothing_is_listed(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.install("claude", "codex")
    fh.claude_mcp(key=KEY_CLAUDE)
    fh.codex_mcp(key=KEY_CODEX)
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    trail = _trail()
    report = _run(fh, trail)
    assert _sent(trail) == [KEY_CLAUDE, KEY_CODEX]
    assert not any("read budget" in u or "GETs" in u for u in report.signals.unchecked)


# ---------------------------------------------------------------------------
# remembra_doctor: the key the MCP server itself uses
# ---------------------------------------------------------------------------


def _payload(fh: FakeHome, own_key: str, trail: FakeTrail | None = None) -> dict:
    env = fh.environ(REMEMBRA_API_KEY=own_key, REMEMBRA_URL=URL, REMEMBRA_AGENT_ID="claude-desktop")
    return tools.doctor_payload(
        None, trail is not None, environ=env, home=fh.home, transport=trail.transport if trail else None, which=fh.which, now=NOW
    )


def test_mcp_servers_own_key_is_named_as_not_checked_when_no_config_holds_it(four_keys: FakeHome) -> None:
    trail = _trail()
    payload = _payload(four_keys, KEY_MCP, trail)
    assert KEY_MCP not in _sent(trail)  # the hooks' keys were asked about, not this server's
    own = [u for u in payload["unchecked"] if "this MCP server" in u]
    assert len(own) == 1 and "health_check" in own[0], payload["unchecked"]
    assert all(key not in json.dumps(payload) for key in ALL_KEYS)


def test_mcp_servers_own_key_is_not_mentioned_when_a_config_holds_the_same_one(four_keys: FakeHome) -> None:
    payload = _payload(four_keys, KEY_CODEX, _trail())
    assert not any("this MCP server" in u for u in payload["unchecked"])


def test_no_own_key_no_note(four_keys: FakeHome) -> None:
    payload = tools.doctor_payload(None, False, environ=four_keys.environ(), home=four_keys.home, which=four_keys.which, now=NOW)
    assert not any("this MCP server" in u for u in payload["unchecked"])
