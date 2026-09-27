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


def test_a_redirect_is_not_an_accepted_key(fh: FakeHome) -> None:
    """The hooks don't follow redirects: an http:// URL in front of an https-only proxy fails every call."""
    fh.credentials(url="http://api.remembra.test")
    fh.hooks("claude-code")
    moved = FakeTrail(
        key_status=301, key_text="", key_headers={"location": "https://api.remembra.test/api/v1/trail/summary?days=7"}
    )
    report = run(fh, moved)
    assert "HOOKS_NOT_FIRING" not in ids(report) and "NOTHING_WAITING" not in ids(report)
    f = only(report, "SERVER_WRONG_URL")
    assert (f.severity, f.inferred, f.marker) == ("blocker", False, "[!!]")
    assert f.what == (
        "api.remembra.test answered HTTP 301 (a redirect) instead of Remembra's API:"
        " the hooks don't follow redirects, so every call fails."
    )
    assert "it redirects to https://api.remembra.test/api/v1/trail/summary" in f.evidence
    assert f.fix is not None and f.fix.command == "remembra-install --all --url https://api.remembra.test"
    assert f.fix.runs_where == "user_terminal"
    key_read = next(r for r in report.signals.reads if r.what == "key")
    assert key_read.ok is False and "accepted" not in key_read.result
    assert report.exit_code == 1
    # A redirect somewhere else (a login page): named, but no server URL is guessed from it.
    moved.key_headers = {"location": "/login?next=%2Fapi"}
    g = only(run(fh, moved), "SERVER_WRONG_URL")
    assert g.fix is not None and g.fix.command is None and "it redirects to http://api.remembra.test/login" in g.evidence
    # It kept the API path, but under a base no command template takes: named, no command.
    moved.key_headers = {"location": "https://api.remembra.test/v%201/api/v1/trail/summary"}
    odd = only(run(fh, moved), "SERVER_WRONG_URL")
    assert odd.fix is not None and odd.fix.command is None and "/v%201" in odd.fix.text
    # A 200 that is a web page, not the API's JSON: not accepted either.
    page = FakeTrail(key_status=200, key_text="<!doctype html><title>Remembra</title>", key_headers={"content-type": "text/html"})
    h = only(run(fh, page), "SERVER_WRONG_URL")
    assert h.what.startswith("api.remembra.test answered HTTP 200 with a page, not Remembra's API")
    assert h.fix is not None and h.fix.command is None
    # Set in the environment, remembra-install can't change it: the fix says where.
    env = run(fh, moved, REMEMBRA_URL="http://api.remembra.test", REMEMBRA_API_KEY=KEY)
    assert "REMEMBRA_URL" in only(env, "SERVER_WRONG_URL").fix.text


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
    # 0.16.1: a configured project names only folders; a new repository gets its own project (whole-release review:
    # the doctor still said every new repository joined it, and pointed at resolve --bind, not projects split).
    assert (
        legacy.what == "Project alpha names only folders that aren't git repositories; each new repository gets its own project."
    )
    assert legacy.evidence == ("project alpha configured in ~/.claude.json",)  # ~/.claude.json holds the key
    assert "One namespace" not in legacy.what and "joins project" not in legacy.what
    assert legacy.fix is not None and legacy.fix.command == "remembra-relay projects split" and not legacy.to_do
    assert "0.16.0" in (legacy.caveat or "")
    split = only(report, "MCP_PROJECT_SPLIT")
    assert (split.severity, split.inferred) == ("warn", False)
    assert split.evidence == ("claude-code reads alpha · codex reads beta · gemini reads alpha",)
    # REMEMBRA_RELAY_PROJECT is the one setting that keeps every new repository in one project.
    env = run(fh, check_server=False, REMEMBRA_RELAY_PROJECT="one-place")
    one = only(env, "LEGACY_NAMESPACE")
    assert one.evidence == ("project one-place configured in REMEMBRA_RELAY_PROJECT",)
    assert one.what.startswith("One namespace:") and "repositories included, joins project one-place" in one.what
    assert "resolve --project <name> --bind" in (one.fix.text if one.fix else "")


def test_remembra_project_alone_is_no_one_namespace_claim(fh: FakeHome) -> None:
    fh.credentials()
    fh.hooks("claude-code")
    only_env = only(run(fh, check_server=False, REMEMBRA_PROJECT="team"), "LEGACY_NAMESPACE")
    assert only_env.evidence == ("project team configured in REMEMBRA_PROJECT",)
    assert only_env.what.startswith("Project team names only folders") and "One namespace" not in only_env.what
    text = run(fh, check_server=False, REMEMBRA_PROJECT="team").text()
    assert "joins project team" not in text and "remembra-relay projects split" in text
    # "default" (a plain install) is no configured project at all.
    assert "LEGACY_NAMESPACE" not in ids(run(fh, check_server=False, REMEMBRA_PROJECT="default"))
    assert "LEGACY_NAMESPACE" not in ids(run(fh, check_server=False))


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
    report = run(fh, check_server=False)
    f = only(report, "UNVERIFIED_NOT_WRITTEN", "cursor")
    # connect --apply leaves it out unless asked: the user's choice, a note (no [!!], not a to-do, exit 0).
    assert (f.severity, f.inferred, f.marker, f.to_do) == ("info", False, "", False)
    assert report.exit_code == 0
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent cursor --include-unverified"
    assert f.what == (
        "Cursor: hooks not written. Cursor's hooks are unverified: its own hook runner fired them,"
        " but no logged-in Cursor session has run them yet."
    )
    assert "connect --apply leaves unverified adapters out unless you add --include-unverified" in f.evidence
    assert not any("dry run" in e for e in f.evidence)
    assert f.caveat and "--agents-md" in f.caveat
    # Some of its hooks written (it was asked for) but not the session's start: incomplete, that needs you.
    fh.hooks("cursor", missing=("sessionStart",))
    g = only(run(fh, check_server=False), "UNVERIFIED_NOT_WRITTEN", "cursor")
    assert (g.severity, g.marker) == ("warn", "[!!]") and "~/.cursor/hooks.json: missing sessionStart" in g.evidence


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
    # The same call and fix as the dashboard's slip and remembra_setup (remembra.marshal.words).
    assert f.what == (
        "Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust. Codex skips untrusted hooks without a message."
    )
    assert f.fix.text == (
        "Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd."
    )
    assert f.doc == "docs.remembra.dev/guides/relay/#codex-trust"
    assert f.evidence == ("~/.codex/hooks.json: 3 remembra-relay hooks", "~/.codex/config.toml: no [hooks.state] entries")
    assert "trust NOT recorded" in report.text()
    # Trust recorded for one hook only: the other two are named.
    fh.codex_mcp()
    fh.trust_codex(trusted=["SessionStart"])
    g = only(run(fh, check_server=False), "CODEX_TRUST_MISSING")
    assert "~/.codex/config.toml: no trust record for UserPromptSubmit, SessionEnd" in g.evidence
    assert g.what.startswith("Codex needs you to trust 2 hooks: ")
    assert g.fix is not None and g.fix.text.endswith("and trust UserPromptSubmit and SessionEnd.")


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


def test_hooks_not_firing_when_others_handed_off_and_nothing_arrives(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0] * 7, "last_active": None}
    trail.items = [entry("claude-code", "handoff", NOW - 2 * 3600)]
    f = only(run(fh, trail), "HOOKS_NOT_FIRING", "codex")
    assert (f.severity, f.inferred, f.marker) == ("warn", True, "[??]")
    assert f.what == "The key works, but nothing from Codex has reached Remembra."  # the slip's sentence
    assert f.fix is not None and f.fix.command == "remembra-relay doctor --agent codex"
    assert f.fix.text == "End one Codex session in a repository. Then run:"
    assert "no entry from codex ever" in f.evidence and "1 handoff from other agents waited for it" in f.evidence
    assert f.doc == "docs.remembra.dev/guides/relay/#doctor"
    # It handed off before, just not this week: the window is named.
    trail.agents["codex"]["last_active"] = "2026-09-10T09:00:00+00:00"
    g = only(run(fh, trail), "HOOKS_NOT_FIRING", "codex")
    assert g.what == "The key works, but nothing from Codex has reached Remembra in 7 days."
    assert "last entry from codex: 16d ago" in g.evidence
    # Untrusted Codex hooks explain the silence already: no second finding.
    (fh.home / ".codex" / "config.toml").write_text("")
    assert "HOOKS_NOT_FIRING" not in ids(run(fh, trail))


def test_nothing_waiting_is_proven_when_no_handoff_waited(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0] * 7, "last_active": None}
    trail.items = []
    report = run(fh, trail)
    f = only(report, "NOTHING_WAITING", "codex")
    assert (f.severity, f.inferred, f.marker) == ("warn", False, "[!!]")
    assert f.what == "Codex hasn't ended a session with the hooks yet, and no handoff was waiting for it."
    assert f.fix is not None and f.fix.command == "remembra-relay doctor --agent codex"
    assert "no handoff from another agent in the last 100 entries" in f.evidence
    assert report.exit_code == 1  # ending one session is still the user's to do
    # The trail could not be read: who waited is unknown, so it is only inferred.
    trail.trail_status = 400
    g = only(run(fh, trail), "HOOKS_NOT_FIRING", "codex")
    assert g.inferred is True and "the trail could not be read: who waited for it is unknown" in g.evidence


def test_picks_up_never_closes_is_proven_by_pickups(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0] * 7, "last_active": None}
    trail.items = [
        entry("claude-code", "handoff", NOW - 2 * 3600, picked=[("codex", NOW - 3600)]),
        entry("claude-code", "handoff", NOW - 5 * 3600, picked=[("codex", NOW - 4 * 3600)]),
    ]
    f = only(run(fh, trail), "PICKS_UP_NEVER_CLOSES", "codex")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.what == "Codex read 2 briefs but never handed off: its close hasn't reached Remembra."
    assert f.fix is not None and f.fix.command == "remembra-relay doctor --agent codex"
    assert f.fix.text == "End one Codex session. If no handoff arrives, doctor names the failing close:"
    assert "trail: codex picked up 2 handoffs in the last 100 entries" in f.evidence
    assert f.caveat and "session is still open" in f.caveat
    # Checkpoints but no handoff: still never closes, and it is said once (no STALE_CHECKPOINT as well).
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0, 0, 0, 0, 0, 0, 1], "last_active": None}
    trail.items.append(entry("codex", "checkpoint", NOW - 3 * 3600))
    report = run(fh, trail)
    assert only(report, "PICKS_UP_NEVER_CLOSES", "codex").what.startswith("Codex read 2 briefs")
    assert "STALE_CHECKPOINT" not in ids(report)


def test_an_idle_agent_that_handed_off_before_is_not_never_closes(fh: FakeHome) -> None:
    """Pickups older than a week and its own handoff after them: idle, not broken (the slip says connected)."""
    trail = healthy(fh)
    trail.agents["codex"] = {"handoffs": 5, "sessions_7d": 0, "daily": [0] * 7, "last_active": "2026-09-17T12:00:00+00:00"}
    trail.items = [
        entry("codex", "handoff", NOW - 9 * 86400),
        entry("claude-code", "handoff", NOW - 11 * 86400, picked=[("codex", NOW - 10 * 86400)]),
    ]
    report = run(fh, trail, agents=["codex"])
    assert "PICKS_UP_NEVER_CLOSES" not in ids(report)
    # Nothing waited for it and it handed off before: nothing is wrong, so nothing is said.
    assert [f for f in report.findings if f.agent == "codex" and f.actionable] == []
    assert report.exit_code == 0
    # Its handoffs all-time count, even when the trail window holds none of them.
    trail.items = [entry("claude-code", "handoff", NOW - 11 * 86400, picked=[("codex", NOW - 10 * 86400)])]
    assert "PICKS_UP_NEVER_CLOSES" not in ids(run(fh, trail, agents=["codex"]))
    # One brief read twice (two sessions) is one brief, as the slip counts it.
    trail.agents["codex"] = {"handoffs": 0, "sessions_7d": 0, "daily": [0] * 7, "last_active": None}
    trail.items = [entry("claude-code", "handoff", NOW - 3 * 3600, picked=[("codex", NOW - 2 * 3600), ("codex", NOW - 3600)])]
    f = only(run(fh, trail, agents=["codex"]), "PICKS_UP_NEVER_CLOSES", "codex")
    assert f.what == "Codex read 1 brief but never handed off: its close hasn't reached Remembra."


def test_a_close_that_worked_after_the_failure_clears_close_failing(fh: FakeHome) -> None:
    """status.json keeps one success slot, so a brief after a fixed close hides it; the trail still shows it."""
    trail = healthy(fh)
    failed = {"command": "close", "ts": NOW - 5 * 3600, "at": "x", "error": "HTTP 422: bad", "http_status": 422}
    fh.status({"claude-code": {"last_success": {"command": "brief", "ts": NOW - 300, "at": "x"}, "last_failure": failed}})
    # The trail holds claude-code's handoff from 2h ago, after the failed close: a close worked since.
    report = run(fh, trail, agents=["claude-code"])
    assert "CLOSE_FAILING" not in ids(report)
    assert "no close has worked since" not in report.text()
    # No handoff since on the trail: the brief worked, the close is untested since. Said, never as proven.
    trail.items = [entry("codex", "handoff", NOW - 3 * 3600, picked=[("claude-code", NOW - 2.5 * 3600)])]
    trail.agents["claude-code"]["last_active"] = "2026-09-26T06:00:00+00:00"
    f = only(run(fh, trail, agents=["claude-code"]), "CLOSE_FAILING", "claude-code")
    assert f.inferred is True and "no close has worked since" not in f.what
    assert f.what == "claude-code's last close failed (HTTP 422) 5h ago; no handoff from it has reached the trail since."
    assert f.fix is not None and f.fix.command == "remembra-relay close --agent claude-code"
    # Without the trail, a later success only leaves a note: nothing is claimed about closes since.
    offline = run(fh, check_server=False, agents=["claude-code"])
    g = only(offline, "CLOSE_FAILING", "claude-code")
    assert (g.severity, g.inferred) == ("info", True) and not g.to_do
    assert "no close has worked since" not in g.what and "a brief worked since" in g.what
    assert "1 thing to do" not in offline.text() and offline.exit_code == 0
    # A retried failure with nothing queued and a later success: the next run sent it. Nothing to say.
    fh.status(
        {
            "claude-code": {
                "last_success": {"command": "brief", "ts": NOW - 300, "at": "x"},
                "last_failure": {"command": "close", "ts": NOW - 5 * 3600, "at": "x", "error": "ConnectError: refused"},
            }
        }
    )
    assert "CLOSE_FAILING" not in ids(run(fh, check_server=False, agents=["claude-code"]))


def test_stale_checkpoint_and_the_per_agent_read(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.items = [entry("claude-code", "checkpoint", NOW - 3 * 3600), entry("codex", "handoff", NOW - 4 * 3600)]
    f = only(run(fh, trail), "STALE_CHECKPOINT", "claude-code")
    assert (f.severity, f.inferred) == ("warn", False)
    assert f.fix is not None and f.fix.command == "remembra-relay close --agent claude-code"
    assert f.fix.text == "In the repository it worked in, write the handoff now:"
    assert f.what == (
        "Claude Code's last session stopped without a handoff."
        " Its newest entry is a checkpoint from 3h ago, with no handoff after it."
    )
    # A checkpoint under an hour old is work in progress.
    trail.items = [entry("claude-code", "checkpoint", NOW - 1800), entry("codex", "handoff", NOW - 4 * 3600)]
    assert "STALE_CHECKPOINT" not in ids(run(fh, trail))
    # Codex is not in the last 100 entries: its own last entries are read (one more GET, never more than four).
    trail.items = [entry("claude-code", "handoff", NOW - 3600)]
    trail.agent_items = {"codex": [entry("codex", "checkpoint", NOW - 20 * 3600)]}
    trail.requests.clear()
    report = run(fh, trail)
    assert only(report, "STALE_CHECKPOINT", "codex").what.startswith("Codex's last session stopped without a handoff")
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


def test_doc_links_are_never_broken_across_lines(fh: FakeHome) -> None:
    """A per-agent finding's page (the longest anchor, under the rail) stays one piece that copies and opens."""
    fh.credentials()
    fh.hooks("codex")
    fh.trust_codex()
    failure = {"command": "close", "ts": NOW - 600, "error": "HTTP 422: bad", "http_status": 422}
    fh.status({"codex": {"last_failure": failure}})
    report = run(fh, FakeTrail(), agents=["codex"])
    f = only(report, "CLOSE_FAILING", "codex")
    assert f.doc == "docs.remembra.dev/guides/relay/#if-the-server-cannot-be-reached"
    for text in (report.text(), report.text(Style(ascii=True)), report.text(Style(color=True))):
        plain = text.replace("\033[2m", "").replace("\033[0m", "")
        assert any(line.strip().endswith(f.doc) for line in plain.splitlines()), text
    for line in report.text().splitlines():
        assert len(line) <= 76 or commands.is_allowed(line.replace("┊", "").strip()), line
    lines = report.text().splitlines()
    doc_at = next(i for i, line in enumerate(lines) if line.strip().endswith(f.doc))
    assert lines[doc_at - 1].strip().endswith("doc") or "doc   docs.remembra.dev" in lines[doc_at]


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
    assert (f.severity, f.inferred) == ("warn", False) and "(HTTP 503)" in f.what
    assert f.fix is not None and f.fix.command is None and "relay.log" in f.fix.text


def test_a_baton_from_an_agent_without_a_station_is_shown_once(fh: FakeHome) -> None:
    trail = healthy(fh)
    trail.agents["windsurf"] = {"handoffs": 1, "daily": [0, 0, 0, 0, 0, 0, 1], "last_active": "2026-09-26T11:30:00+00:00"}
    trail.items = [entry("windsurf", "handoff", NOW - 1800), *trail.items]
    text = run(fh, trail).text()
    assert text.count("◆") == 1
    assert "◆ last handoff from windsurf 30m ago · not picked up by another agent yet" in text
