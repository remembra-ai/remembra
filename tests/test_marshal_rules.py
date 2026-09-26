"""Every doctor rule, from a fake HOME and a fake trail, through ``remembra.marshal.doctor.run``.

Each test builds the files the real tools write (hook files through the
relay's adapters, queue entries through ``relay.outbox``, Codex trust as
``/hooks`` records it) and asserts the finding's id, severity, whether it is
proven or inferred, where its fix runs and the exact command.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from remembra.marshal import commands, doctor, signals
from remembra.marshal.render import Style
from remembra.marshal.rules import RULE_IDS, Finding
from tests.marshal_fixtures import CLOUDFLARE_PAGE, KEY, NOW, URL, FakeHome, FakeTrail, entry


@pytest.fixture()
def fh(tmp_path: Path) -> FakeHome:
    return FakeHome(tmp_path)


def run(fh: FakeHome, trail: FakeTrail | None = None, agents: list[str] | None = None, check_server: bool = True, **env: str):
    return doctor.run(
        fh.home,
        agents,
        check_server,
        environ=fh.environ(**env),
        transport=trail.transport if trail else httpx.MockTransport(lambda r: httpx.Response(599)),
        which=fh.which,
        now=NOW,
        cwd=fh.root,
    )


def only(report: doctor.Report, rule: str, agent: str | None = "any") -> Finding:
    found = [f for f in report.findings if f.id == rule and (agent == "any" or f.agent == agent)]
    assert len(found) == 1, [(f.id, f.agent, f.what) for f in report.findings]
    return found[0]


def ids(report: doctor.Report) -> list[str]:
    return [f.id for f in report.findings]


def healthy(fh: FakeHome) -> FakeTrail:
    """Claude Code and Codex wired and trusted, a key saved, both agents active on the trail."""
    fh.credentials()
    fh.claude_mcp()
    fh.codex_mcp()
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    return FakeTrail(
        agents={
            "claude-code": {
                "handoffs": 4,
                "sessions_7d": 3,
                "daily": [0, 1, 1, 0, 1, 0, 1],
                "last_active": "2026-09-26T10:00:00+00:00",
            },
            "codex": {
                "handoffs": 2,
                "sessions_7d": 2,
                "daily": [0, 0, 1, 0, 0, 1, 0],
                "last_active": "2026-09-26T09:00:00+00:00",
            },
        },
        items=[
            entry("claude-code", "handoff", NOW - 2 * 3600, picked=[("codex", NOW - 3600)]),
            entry("codex", "handoff", NOW - 3 * 3600, picked=[("claude-code", NOW - 2.5 * 3600)]),
        ],
    )


# ---------------------------------------------------------------------------
# A clean machine
# ---------------------------------------------------------------------------


def test_a_healthy_machine_has_nothing_to_do(fh: FakeHome) -> None:
    trail = healthy(fh)
    report = run(fh, trail)
    assert [f for f in report.findings if f.actionable] == []
    assert report.exit_code == 0
    text = report.text()
    assert "Nothing to do." in text and text.rstrip().endswith("Nothing was changed.")
    assert "◆ last handoff 2h ago · picked up by codex 60m ago" in text
    assert "● claude-code" in text and "● codex" in text and "trusted" in text
    assert "· gemini       not detected on this machine · skipped" in text
    # Reads: one key check, one trail read (both agents are in the window), nothing else.
    assert [(r.method, r.url.path, dict(r.url.params)) for r in trail.requests] == [
        ("GET", "/api/v1/trail/summary", {"days": "7"}),
        ("GET", "/api/v1/trail", {"limit": "100"}),
    ]
    assert trail.requests[0].headers["x-api-key"] == KEY
    assert trail.requests[0].headers["user-agent"].startswith("remembra-doctor/")
    assert "x-remembra-agent-id" not in trail.requests[0].headers  # the doctor never poses as an agent
    assert KEY not in text and "SECRET HEADLINE" not in text
    assert KEY not in json.dumps(report.json()) and "SECRET HEADLINE" not in json.dumps(report.json())


def test_every_rule_id_is_documented_and_tested_here() -> None:
    source = Path(__file__).read_text()
    guide = (Path(__file__).resolve().parents[1] / "docs" / "guides" / "relay.md").read_text()
    for rule in RULE_IDS:
        assert f'"{rule}"' in source, rule
        assert f"`{rule}`" in guide, rule
    assert len(RULE_IDS) >= 14


# ---------------------------------------------------------------------------
# Key and server
# ---------------------------------------------------------------------------


def test_key_missing_sends_the_user_to_their_own_terminal(fh: FakeHome) -> None:
    fh.hooks("claude-code")
    report = run(fh, FakeTrail())
    f = only(report, "KEY_MISSING")
    assert (f.severity, f.inferred, f.agent) == ("blocker", False, None)
    assert f.fix is not None and f.fix.runs_where == "user_terminal"
    assert f.fix.command == "remembra-install --all"  # keeps a saved server; Remembra Cloud on a first install
    assert report.exit_code == 1
    self_hosted = run(fh, FakeTrail(), REMEMBRA_URL="https://memory.example.org")
    assert only(self_hosted, "KEY_MISSING").fix.command == "remembra-install --all --url https://memory.example.org"


def test_key_rejected_live_is_proven_and_recorded_is_inferred(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    report = run(fh, FakeTrail(key_status=401))
    f = only(report, "KEY_REJECTED")
    assert (f.severity, f.inferred) == ("blocker", False)
    assert f.fix is not None and f.fix.runs_where == "user_terminal"
    assert f.fix.command == f"remembra-install --all --url {URL}"
    assert "api.remembra.test said: HTTP 401: Invalid API key" in f.evidence
    assert report.exit_code == 1
    creds = f"credentials:{fh.home / '.remembra' / 'credentials'}"
    fh.status({}, keys={creds: {"state": "rejected", "http_status": 401, "at": "x", "ts": NOW - 3600}})
    offline = run(fh, check_server=False)
    g = only(offline, "KEY_REJECTED")
    assert g.inferred is True and "the hooks last got HTTP 401 with this key 60m ago" in g.evidence
    assert offline.exit_code == 0  # inferred findings are [??], not [!!]


def test_key_refused_points_at_the_dashboard(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    report = run(fh, FakeTrail(key_status=403, key_body={"detail": "Key not allowed for agent codex"}))
    f = only(report, "KEY_REFUSED")
    assert (f.severity, f.inferred) == ("blocker", False)
    assert f.fix is not None and f.fix.runs_where == "dashboard" and f.fix.command is None


def test_a_cloudflare_403_is_the_firewall_not_the_key(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    report = run(fh, FakeTrail(html_403=True))
    assert "KEY_REFUSED" not in ids(report)
    f = only(report, "KEY_FIREWALL")
    assert (f.severity, f.inferred) == ("warn", False)
    assert "Blocked by the server's firewall" in f.what
    assert "Cloudflare Ray ID 8c1f2e3d4a5b6c7d" in f.evidence
    assert f.fix is not None and f.fix.command is None and "remembra.dev/contact" in f.fix.text
    key_read = next(r for r in report.signals.reads if r.what == "key")
    assert key_read.result.startswith("blocked by api.remembra.test's firewall (HTTP 403, not Remembra)")


def test_server_unreachable_is_a_warning_with_the_url(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    report = run(fh, FakeTrail(error=httpx.ConnectError("connection refused")))
    f = only(report, "SERVER_UNREACHABLE")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.fix is not None and URL in f.fix.text
    assert any("server unreachable (ConnectError" in e for e in f.evidence)
    assert "your trail (the server did not accept a read)" in report.signals.unchecked


# ---------------------------------------------------------------------------
# Queue and closes
# ---------------------------------------------------------------------------


def test_outbox_groups_by_cause(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    fh.queue("codex", "s-401", error="HTTP 401: Invalid API key", status=401)
    fh.queue("codex", "s-429", error="HTTP 429: relay rate limit exceeded", status=429)
    fh.queue("claude-code", "s-net", error="ConnectError: [Errno 61] Connection refused")
    fh.queue("codex", "s-cf", error="HTTP 403: " + CLOUDFLARE_PAGE[:280], status=403)
    report = run(fh, FakeTrail(key_status=401))
    queued = {f.what.split(": ", 1)[1]: f for f in report.findings if f.id == "OUTBOX_QUEUED"}
    rejected = queued["the server rejected the key (HTTP 401)."]
    assert (rejected.severity, rejected.fix.runs_where, rejected.fix.command) == (
        "blocker",
        "user_terminal",
        f"remembra-install --all --url {URL}",
    )
    limited = queued["rate-limited (HTTP 429)."]
    assert (
        limited.severity == "warn"
        and "Relay Free allows 30 relay events a minute and 300 unenriched writes a day" in limited.evidence
    )
    network = queued["the server couldn't be reached."]
    assert network.severity == "warn" and network.fix is not None and network.fix.command is None
    firewall = queued["blocked by the server's firewall (an HTML 403, not Remembra)."]
    assert firewall.severity == "warn" and "Cloudflare Ray ID 8c1f2e3d4a5b6c7d" in firewall.evidence
    assert "› outbox  4 waiting · 0 held" in report.text()
    # With the key accepted now, the queued 401s just wait for the next brief or close.
    accepted = run(fh, FakeTrail())
    again = [f for f in accepted.findings if f.id == "OUTBOX_QUEUED" and "401" in f.what]
    assert len(again) == 1 and again[0].severity == "warn" and "the key is accepted now" in again[0].what


def test_a_held_entry_gets_the_one_file_to_drop(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    path = fh.queue("codex", "s-old", url="https://old-server.example")
    report = run(fh, FakeTrail())
    f = only(report, "OUTBOX_HELD")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.fix is not None and f.fix.command == f"rm ~/.remembra/relay/outbox/{path.name}"
    assert f.fix.runs_where == "agent_ok" and f.fix.writes == (f"~/.remembra/relay/outbox/{path.name}",)
    assert any("it is for https://old-server.example" in e for e in f.evidence)
    assert "OUTBOX_QUEUED" not in ids(report)


def test_close_failing_from_status_and_from_the_background_log(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex()
    fh.status(
        {
            "codex": {
                "last_success": {"command": "close", "ts": NOW - 5 * 3600, "at": "x"},
                "last_failure": {"command": "close", "ts": NOW - 3600, "at": "x", "error": "HTTP 401: nope", "http_status": 401},
            }
        }
    )
    f = only(run(fh, FakeTrail(), agents=["codex"]), "CLOSE_FAILING", "codex")
    assert (f.severity, f.inferred) == ("blocker", False)
    assert f.fix is not None and f.fix.runs_where == "user_terminal" and f.fix.command == f"remembra-install --all --url {URL}"
    # A brief that worked after the failed close hides whether a close worked in between: inferred.
    fh.status(
        {
            "codex": {
                "last_success": {"command": "brief", "ts": NOW - 600, "at": "x"},
                "last_failure": {"command": "close", "ts": NOW - 3600, "at": "x", "error": "ValueError: boom"},
            }
        }
    )
    g = only(run(fh, FakeTrail(), agents=["codex"]), "CLOSE_FAILING", "codex")
    assert g.inferred is True and g.fix is not None and g.fix.command == "remembra-relay close --agent codex"
    # The detached close's log: shown (at most 20 lines), with a planted key redacted.
    fh.status({})
    lines = [f"line {i}" for i in range(30)] + [f"remembra-relay: close failed: HTTP 401: bad key {KEY}"]
    fh.close_log(lines, NOW - 1800)
    report = run(fh, FakeTrail(), agents=["codex"])
    log = only(report, "CLOSE_FAILING", None)
    assert log.severity == "blocker" and log.evidence[0] == "~/.remembra/relay/last-detached-close.log (secrets redacted):"
    assert len(log.evidence) == 21 and "[REDACTED:" in log.evidence[-1]
    assert KEY not in report.text() and KEY not in json.dumps(report.json())


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_legacy_namespace_and_split_projects(fh: FakeHome) -> None:
    fh.credentials(project="acme")
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    fh.claude_mcp(project="alpha")
    fh.codex_mcp(project="beta")
    fh.trust_codex()
    fh.install("gemini")
    (fh.home / ".gemini").mkdir()
    (fh.home / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"remembra": {"command": "x", "env": {"REMEMBRA_PROJECT": "alpha"}}}})
    )
    report = run(fh, check_server=False)
    legacy = only(report, "LEGACY_NAMESPACE")
    assert (legacy.severity, legacy.inferred) == ("info", False) and legacy.marker == ""
    assert "joins project alpha" in legacy.what  # ~/.claude.json holds the key, so its project is the one used
    split = only(report, "MCP_PROJECT_SPLIT")
    assert (split.severity, split.inferred) == ("warn", False)
    assert split.evidence == ("claude-code reads alpha · codex reads beta · gemini reads alpha",)
    env = run(fh, check_server=False, REMEMBRA_RELAY_PROJECT="one-place")
    assert "configured in REMEMBRA_RELAY_PROJECT" in only(env, "LEGACY_NAMESPACE").evidence[0]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_not_detected_only_when_asked_for(fh: FakeHome) -> None:
    fh.credentials()
    assert "NOT_DETECTED" not in ids(run(fh, check_server=False))
    f = only(run(fh, check_server=False, agents=["gemini"]), "NOT_DETECTED")
    assert (f.severity, f.agent) == ("info", "gemini") and f.evidence == ("no gemini on PATH",)


def test_config_unreadable(fh: FakeHome) -> None:
    fh.credentials()
    path = fh.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    f = only(run(fh, check_server=False), "CONFIG_UNREADABLE")
    assert (f.severity, f.agent, f.inferred) == ("blocker", "claude-code", False)


def test_hooks_not_written_says_connect_only_ran_dry(fh: FakeHome) -> None:
    fh.credentials()
    fh.claude_mcp()
    (fh.home / ".claude").mkdir()
    (fh.home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
    report = run(fh, check_server=False)
    f = only(report, "HOOKS_NOT_WRITTEN", "claude-code")
    assert (f.severity, f.inferred) == ("blocker", False)
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent claude-code"
    assert f.fix.runs_where == "agent_ok" and f.fix.writes == ("~/.claude/settings.json",) and f.fix.backup
    assert "~/.claude/settings.json: no remembra-relay hooks" in f.evidence
    assert "~/.claude.json: remembra MCP server set up (remembra-install ran)" in f.evidence
    assert any("dry run only, or never run" in e for e in f.evidence)
    (fh.home / ".claude" / "settings.json.bak-relay-20260920-101010").write_text("{}")
    g = only(run(fh, check_server=False), "HOOKS_NOT_WRITTEN", "claude-code")
    assert "settings.json.bak-relay-20260920-101010: connect --apply wrote here once; the hooks were removed since" in g.evidence


def test_unverified_adapter_not_written(fh: FakeHome) -> None:
    fh.credentials()
    (fh.home / ".cursor").mkdir()
    f = only(run(fh, check_server=False), "UNVERIFIED_NOT_WRITTEN", "cursor")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent cursor --include-unverified"
    assert "never run against the real tool" in f.what and f.caveat and "--agents-md" in f.caveat


def test_hooks_that_call_a_missing_command(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code", relay="/gone/venv/bin/remembra-relay")
    f = only(run(fh, check_server=False), "HOOKS_STALE_COMMAND", "claude-code")
    assert (f.severity, f.inferred) == ("blocker", False)
    assert "/gone/venv/bin/remembra-relay" in f.what
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent claude-code"


def test_hooks_from_an_older_connect(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code", missing=("StopFailure", "PreCompact"))
    f = only(run(fh, check_server=False), "HOOKS_INCOMPLETE", "claude-code")
    assert (f.severity, f.inferred) == ("warn", False)
    assert "StopFailure, PreCompact missing" in f.what
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent claude-code"


# ---------------------------------------------------------------------------
# Codex trust and automations
# ---------------------------------------------------------------------------


def test_codex_trust_missing(fh: FakeHome) -> None:
    fh.credentials()
    fh.codex_mcp()
    fh.hooks("codex")
    report = run(fh, check_server=False)
    f = only(report, "CODEX_TRUST_MISSING")
    assert (f.severity, f.inferred, f.agent) == ("blocker", False, "codex")
    assert f.fix is not None and f.fix.runs_where == "codex_ui" and f.fix.command is None
    assert "Codex Settings > Hooks" in f.fix.text and "/hooks" in f.fix.text
    assert f.evidence == ("~/.codex/hooks.json: 3 remembra-relay hooks", "~/.codex/config.toml: no [hooks.state] entries")
    assert "trust NOT recorded" in report.text()
    # Trust recorded for one hook only: the other two are named.
    fh.codex_mcp()
    fh.trust_codex(trusted=["SessionStart"])
    g = only(run(fh, check_server=False), "CODEX_TRUST_MISSING")
    assert "~/.codex/config.toml: no trust record for UserPromptSubmit, SessionEnd" in g.evidence


def test_codex_trust_keys_match_through_symlinks(fh: FakeHome, tmp_path: Path) -> None:
    """Codex writes the key with the resolved path when CODEX_HOME is set, the given path otherwise."""
    fh.credentials()
    fh.hooks("codex")
    link = tmp_path / "link-home"
    link.symlink_to(fh.home)
    fh.trust_codex(key_path=str(link / ".codex" / "hooks.json"))
    assert "CODEX_TRUST_MISSING" not in ids(run(fh, check_server=False))


def test_codex_trust_stale_is_inferred(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex(stale=["UserPromptSubmit"])
    f = only(run(fh, check_server=False), "CODEX_TRUST_STALE")
    assert (f.severity, f.inferred) == ("blocker", True) and f.marker == "[??]"
    assert "older version of UserPromptSubmit" in f.what


def test_codex_hook_disabled(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex(disabled=["SessionEnd"])
    f = only(run(fh, check_server=False), "CODEX_HOOK_DISABLED")
    assert (f.severity, f.inferred) == ("blocker", False) and f.fix is not None and f.fix.runs_where == "codex_ui"


def test_codex_config_that_cannot_be_read_is_unchecked_never_trusted(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    (fh.home / ".codex" / "config.toml").write_text("[hooks.state\nbroken = ")
    report = run(fh, check_server=False)
    f = only(report, "CODEX_TRUST_UNCHECKED")
    assert (f.severity, f.inferred) == ("info", True)
    assert "trust unchecked" in report.text()
    assert any("Codex hook trust" in u for u in report.signals.unchecked)
    trust = report.signals.codex.trust
    assert trust is not None and not trust.all_trusted


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
def test_codex_config_without_read_permission_is_unchecked(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    config = fh.codex_mcp()
    fh.trust_codex()
    config.chmod(0)
    try:
        f = only(run(fh, check_server=False), "CODEX_TRUST_UNCHECKED")
        assert "PermissionError" in f.evidence[1]
    finally:
        config.chmod(0o600)


def test_codex_automations(fh: FakeHome, monkeypatch: pytest.MonkeyPatch) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex()
    fh.rollout("automation", 5)
    fh.rollout("user", 2)
    fh.rollout("subagent", 1)
    fh.rollout("automation", 3, age_days=9)  # older than the 7-day window
    monkeypatch.setattr(signals, "automation_skip_supported", lambda: False)
    report = run(fh, check_server=False)
    f = only(report, "CODEX_AUTOMATIONS")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.what.startswith("5 Codex automation runs in 7 days, and this install runs the relay hooks for each")
    assert f.fix is not None and f.fix.command == commands.PIPX_INSTALL
    assert report.signals.codex.subagents_7d == 1
    monkeypatch.setattr(signals, "automation_skip_supported", lambda: True)
    g = only(run(fh, check_server=False), "CODEX_AUTOMATIONS")
    assert g.severity == "info" and "the relay skips them" in g.what
    h = only(run(fh, check_server=False, REMEMBRA_RELAY_INCLUDE_AUTOMATIONS="1"), "CODEX_AUTOMATIONS")
    assert h.severity == "info" and "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS" in h.what
    # Untrusted hooks never run, so automations leave nothing: no finding.
    (fh.home / ".codex" / "config.toml").write_text("")
    assert "CODEX_AUTOMATIONS" not in ids(run(fh, check_server=False))


# ---------------------------------------------------------------------------
# Trail
# ---------------------------------------------------------------------------


def test_server_no_entries_inferred_or_proven_by_pickups(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0] * 7, "last_active": None}
    trail.items = [entry("claude-code", "handoff", NOW - 2 * 3600)]
    f = only(run(fh, trail), "SERVER_NO_ENTRIES", "codex")
    assert (f.severity, f.inferred) == ("warn", True)
    assert f.fix is not None and f.fix.command == "remembra-relay doctor --agent codex"
    assert "no entry from codex ever" in f.evidence and "1 handoff from other agents waited for it" in f.evidence
    trail.items = [entry("claude-code", "handoff", NOW - 2 * 3600, picked=[("codex", NOW - 3600)])]
    g = only(run(fh, trail), "SERVER_NO_ENTRIES", "codex")
    assert g.inferred is False and "reads briefs but no handoff from it reached Remembra" in g.what
    # Untrusted Codex hooks explain the silence already: no second finding.
    (fh.home / ".codex" / "config.toml").write_text("")
    assert "SERVER_NO_ENTRIES" not in ids(run(fh, trail))


def test_stale_checkpoint_and_the_per_agent_read(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.items = [entry("claude-code", "checkpoint", NOW - 3 * 3600), entry("codex", "handoff", NOW - 4 * 3600)]
    f = only(run(fh, trail), "STALE_CHECKPOINT", "claude-code")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.fix is not None and f.fix.command == "remembra-relay close --agent claude-code"
    assert "checkpoint, 3h ago" in f.what
    # A checkpoint under an hour old is work in progress.
    trail.items = [entry("claude-code", "checkpoint", NOW - 1800), entry("codex", "handoff", NOW - 4 * 3600)]
    assert "STALE_CHECKPOINT" not in ids(run(fh, trail))
    # Codex is not in the last 100 entries: its own last entries are read (one more GET, never more than four).
    trail.items = [entry("claude-code", "handoff", NOW - 3600)]
    trail.agent_items = {"codex": [entry("codex", "checkpoint", NOW - 20 * 3600)]}
    trail.requests.clear()
    report = run(fh, trail)
    assert only(report, "STALE_CHECKPOINT", "codex").what.startswith("codex's last session stopped without a handoff")
    assert [dict(r.url.params) for r in trail.requests][-1] == {"agent_id": "codex", "limit": "5"}
    assert len(trail.requests) <= signals.MAX_GETS


def test_a_trail_the_key_may_not_read_is_named_not_guessed(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.trail_status = 400
    report = run(fh, trail)
    assert "pickups and last entries (the trail could not be read)" in report.signals.unchecked
    assert "couldn't read (HTTP 400" in report.text()
    assert "STALE_CHECKPOINT" not in ids(report)


def test_rendering_rules(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    (fh.home / ".cursor").mkdir()
    fh.queue("codex", "s-429", error="HTTP 429: slow down", status=429)
    report = run(fh, FakeTrail(key_status=401))
    plain = report.text()
    assert "\033[" not in plain
    for line in plain.splitlines():
        if len(line) > 76:  # only a command may run past the width (it is never wrapped)
            assert commands.is_allowed(line.replace("┊", "").strip()), line
    assert plain.rstrip().endswith("Nothing was changed.")
    assert "things to do." in plain
    colored = report.text(Style(color=True, truecolor=True))
    orange = "\033[38;2;255;107;43m"
    assert orange in colored  # signal orange marks the fix and nothing else here
    for line in colored.splitlines():
        if orange in line:
            plain_line = line.replace(orange, "").replace("\033[0m", "")
            assert not any(label in plain_line for label in ("seen  ", "then  ", "doc   ", "› ")), plain_line
    ascii_text = report.text(Style(ascii=True))
    assert all(ord(c) < 128 for c in ascii_text), [c for c in ascii_text if ord(c) >= 128][:5]


def test_a_retried_close_failure_is_reported_once(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex()
    fh.status({"codex": {"last_failure": {"command": "close", "ts": NOW - 600, "error": "ConnectError: refused"}}})
    fh.queue("codex", "s-1", error="ConnectError: refused")
    report = run(fh, FakeTrail(), agents=["codex"])
    assert "CLOSE_FAILING" not in ids(report)
    assert only(report, "OUTBOX_QUEUED").what == "1 handoff queued on this machine: the server couldn't be reached."
    assert "1 to watch." in report.text()


def test_a_retried_failure_with_nothing_queued_is_a_warning(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex()
    fh.status({"codex": {"last_failure": {"command": "close", "ts": NOW - 600, "error": "HTTP 503: down", "http_status": 503}}})
    f = only(run(fh, FakeTrail(), agents=["codex"]), "CLOSE_FAILING", "codex")
    assert (f.severity, f.inferred) == ("warn", False) and "(server)" in f.what
    assert f.fix is not None and f.fix.command is None and "relay.log" in f.fix.text


def test_a_baton_from_an_agent_without_a_station_is_shown_once(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["windsurf"] = {"handoffs": 1, "daily": [0, 0, 0, 0, 0, 0, 1], "last_active": "2026-09-26T11:30:00+00:00"}
    trail.items = [entry("windsurf", "handoff", NOW - 1800), *trail.items]
    text = run(fh, trail).text()
    assert text.count("◆") == 1
    assert "◆ last handoff from windsurf 30m ago · not picked up by another agent yet" in text
