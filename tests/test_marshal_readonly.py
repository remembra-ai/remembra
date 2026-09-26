"""Marshal changes nothing: every file under HOME is byte-for-byte the same after each entry point runs,
and the only requests it makes are GETs of the two allow-listed trail paths (never a brief, a recall,
a resolve or a write).

The fake HOME holds everything the relay leaves behind, including the two things the relay's own
``outbox.pending`` would rewrite on a read: a stale ``.sending-`` claim and an unreadable entry.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from remembra.marshal import doctor, signals, tools
from remembra.relay import cli
from tests.marshal_fixtures import KEY, NOW, FakeHome, FakeTrail, entry, tree_digest

FORBIDDEN = ("/api/v1/session/brief", "/api/v1/projects/resolve", "/api/v1/memories/recall", "/api/v1/session/close")


@pytest.fixture()
def busy(tmp_path: Path) -> FakeHome:
    fh = FakeHome(tmp_path)
    fh.credentials(project="acme")
    fh.claude_mcp(project="alpha")
    fh.codex_mcp(project="beta")
    fh.hooks("claude-code", missing=("PreCompact",))
    fh.hooks("codex")
    fh.trust_codex(stale=["UserPromptSubmit"])
    (fh.home / ".cursor").mkdir()
    fh.queue("codex", "s-401", error="HTTP 401: bad key", status=401)
    fh.queue("claude-code", "s-held", url="https://old.example")
    outbox_dir = fh.home / ".remembra" / "relay" / "outbox"
    (outbox_dir / "codex-s-9.json.sending-123-abc").write_text("{}")  # a stale claim pending() would restore
    (outbox_dir / "broken.json").write_text("{not json")  # pending() would rename it to .corrupt
    fh.status(
        {"codex": {"last_failure": {"command": "close", "ts": NOW - 60, "error": "HTTP 500", "http_status": 500}}},
        keys={f"credentials:{fh.home / '.remembra' / 'credentials'}": {"state": "accepted", "ts": NOW - 60}},
    )
    fh.close_log(["remembra-relay: close failed: Traceback (most recent call last):"], NOW - 30)
    fh.rollout("automation", 3)
    return fh


def _trail() -> FakeTrail:
    return FakeTrail(
        agents={"claude-code": {"handoffs": 1, "daily": [0, 0, 0, 0, 0, 0, 1], "last_active": "2026-09-26T11:00:00+00:00"}},
        items=[entry("claude-code", "checkpoint", NOW - 5 * 3600)],
        agent_items={"codex": [entry("codex", "handoff", NOW - 9 * 86400)]},
    )


def _assert_only_allowed_gets(trail: FakeTrail) -> None:
    assert trail.requests, "the server check made no request"
    assert len(trail.requests) <= signals.MAX_GETS
    for request in trail.requests:
        assert request.method == "GET", request
        assert request.url.path in signals.ALLOWED_GETS, request.url
        assert not request.url.path.startswith(FORBIDDEN)
        assert request.content == b""


def test_doctor_run_changes_nothing(busy: FakeHome) -> None:
    before = tree_digest(busy.root)
    trail = _trail()
    report = doctor.run(busy.home, None, True, environ=busy.environ(), transport=trail.transport, which=busy.which, now=NOW)
    assert tree_digest(busy.root) == before
    _assert_only_allowed_gets(trail)
    ids = {f.id for f in report.findings}
    assert {"OUTBOX_QUEUED", "OUTBOX_HELD", "CODEX_TRUST_STALE", "HOOKS_INCOMPLETE", "CLOSE_FAILING"} <= ids
    assert report.signals.outbox_unreadable == ("broken.json",)
    text = report.text()
    assert KEY not in text and KEY not in json.dumps(report.json()) and "SECRET HEADLINE" not in text


def test_mcp_payloads_change_nothing(busy: FakeHome) -> None:
    before = tree_digest(busy.root)
    trail = _trail()
    doctor_payload = tools.doctor_payload(
        None, True, environ=busy.environ(), home=busy.home, transport=trail.transport, which=busy.which, now=NOW
    )
    setup_payload = tools.setup_payload(
        None, environ=busy.environ(), home=busy.home, which=busy.which, os_id="macos", shell="zsh"
    )
    help_payload = tools.help_payload("how do I uninstall")
    assert tree_digest(busy.root) == before
    _assert_only_allowed_gets(trail)
    for payload in (doctor_payload, setup_payload, help_payload):
        dumped = json.dumps(payload)
        assert payload["status"] == "ok" and KEY not in dumped and "rem_" not in dumped.replace("remembra", "")


def test_the_cli_and_the_mcp_tool_change_nothing(
    busy: FakeHome, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("REMEMBRA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(busy.home))
    monkeypatch.setenv("PATH", str(busy.bin))
    before = tree_digest(busy.root)
    code = cli.main(["doctor", "--no-server"])
    out = capsys.readouterr().out
    assert code == 1 and out.rstrip().endswith("Nothing was changed.")
    code = cli.main(["doctor", "--no-server", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["changed_nothing"] is True

    import remembra.mcp.server as server

    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    result = asyncio.run(server.mcp.call_tool("remembra_doctor", {"check_server": False}))
    payload = json.loads(result[0][0].text)  # type: ignore[index]
    assert payload["status"] == "ok" and payload["changed_nothing"] is True
    asyncio.run(server.mcp.call_tool("remembra_setup", {}))
    assert tree_digest(busy.root) == before


def test_the_reader_refuses_anything_but_the_two_trail_gets() -> None:
    budget = signals._Budget(max_gets=2, seconds=5)
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    reader = signals.ServerReader("https://api.remembra.test", KEY, budget, transport)
    for path in FORBIDDEN + ("/api/v1/keys", "/api/v1/trail/summary/../../keys"):
        with pytest.raises(ValueError):
            reader.get(path, {})
    reader.get("/api/v1/trail/summary", {"days": 7})
    reader.get("/api/v1/trail", {"limit": 100})
    with pytest.raises(RuntimeError):
        reader.get("/api/v1/trail", {"limit": 100})
    assert [r.url.path for r in seen] == ["/api/v1/trail/summary", "/api/v1/trail"]


# ---------------------------------------------------------------------------
# A key where it doesn't belong never reaches any output
# ---------------------------------------------------------------------------


def _all_outputs(fh: FakeHome, check_server: bool = True) -> dict[str, str]:
    """The slip, the CLI's JSON and the three MCP payloads, as text."""
    report = doctor.run(fh.home, None, check_server, environ=fh.environ(), which=fh.which, now=NOW, cwd=fh.root)
    return {
        "text": report.text(),
        "json": json.dumps(report.json()),
        "remembra_doctor": json.dumps(
            tools.doctor_payload(None, check_server, environ=fh.environ(), home=fh.home, which=fh.which, now=NOW)
        ),
        "remembra_setup": json.dumps(
            tools.setup_payload(None, environ=fh.environ(), home=fh.home, which=fh.which, os_id="macos", shell="zsh")
        ),
        "remembra_help": json.dumps(tools.help_payload("my handoffs are queued and not sent")),
    }


@pytest.mark.parametrize("where", ["claude_mcp", "credentials"])
def test_a_key_in_the_url_field_is_never_shown(tmp_path: Path, where: str) -> None:
    """URL and key swapped by hand (the Qwen and Kimi docs ask for a hand-edited MCP block)."""
    fh = FakeHome(tmp_path)
    getattr(fh, where)(url=KEY, key="https://api.remembra.dev")
    fh.hooks("claude-code")
    fh.queue("claude-code", "s-1", url=KEY, error="ConnectError: refused")
    for check_server in (True, False):
        outputs = _all_outputs(fh, check_server)
        for name, text in outputs.items():
            assert KEY not in text, (name, check_server)
    report = doctor.run(fh.home, None, True, environ=fh.environ(), which=fh.which, now=NOW, cwd=fh.root)
    assert report.json()["config"]["url"] == signals.NOT_A_URL
    f = next(f for f in report.findings if f.id == "SERVER_UNREACHABLE")
    assert f.what.startswith("The server URL in ") and "is not a URL" in f.what
    assert f.fix is not None and "swapped" in f.fix.text


def test_an_inline_key_in_a_hook_command_is_never_shown(tmp_path: Path) -> None:
    """A hand-wrapped hook (`env REMEMBRA_API_KEY=... remembra-relay`) whose first binary is gone."""
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.hooks("claude-code", relay=f"/gone/bin/env REMEMBRA_API_KEY={KEY} remembra-relay")
    report = doctor.run(fh.home, None, False, environ=fh.environ(), which=fh.which, now=NOW, cwd=fh.root)
    f = next(f for f in report.findings if f.id == "HOOKS_STALE_COMMAND")
    assert "/gone/bin/env" in f.what
    assert f.evidence == ("~/.claude/settings.json: the relay hooks call /gone/bin/env", "/gone/bin/env: no such file")
    for name, text in _all_outputs(fh, check_server=False).items():
        assert KEY not in text, name
