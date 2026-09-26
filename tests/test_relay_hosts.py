"""remembra-relay: hooks another agent runs (routing, C1) and the close dedupe (C2).

Grok Build, Cursor, Devin and Continue load Claude Code's ``~/.claude/settings.json``
hooks; ``gemini hooks migrate``, ``kimi migrate`` and Grok's ``/import-claude``
copy them. Each host is replayed here with its own payload and environment
(``tests/fixtures/relay/hosts``: recorded runs where the agent could be run,
else built from its source or docs; each file says which) through the real
CLI in a subprocess, against the production API routes on a local server, or
against a recording server when the point is that no request is made:

- the host is named by its marker, and a genuine Claude Code payload, even
  with another agent's variables in its environment, stays ``claude-code``;
- a brief another agent runs prints nothing and makes no request;
- a close another agent runs is filed under that agent, with its cwd read by
  that agent's payload mapping (a Cursor ``workspace_roots``, not the
  ``~/.claude`` the hook runs in), or does nothing when the relay has no adapter
  for it (Grok, Devin, Continue), and never parses a foreign transcript;
- Gemini CLI's three SessionEnds on exit make one POST, its orphaned
  empty-stdin one none, and Claude Code's StopFailure then SessionEnd two.
"""

from __future__ import annotations

import copy
import dataclasses
import http.server
import io
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from remembra.relay import cli, hosts, outbox
from remembra.relay import facts as factlib
from remembra.relay.adapters import REGISTRY
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones
from tests.test_relay_cli_e2e import SRC, _api, relay, server  # noqa: F401  (server is a fixture)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOSTS = FIXTURES / "relay" / "hosts"
CLAUDE_FIXTURES = FIXTURES / "claude_code"
CODEX_FIXTURES = FIXTURES / "relay" / "codex"
FIXTURE_REPO = "/home/dev/work/widget"
FIXTURE_HOME = "/home/dev"
CLAUDE = REGISTRY["claude-code"]
CLAUDE_HOOK = ("--hook", "claude-code", "--agent", "claude-code")
ALL_HOSTS = ("grok", "cursor", "kimi", "gemini", "qwen", "devin", "continue")
ROUTED = ("cursor", "kimi", "gemini", "qwen")  # hosts the relay has an adapter for
DROPPED = ("grok", "devin", "continue")  # hosts it has none for (yet)


@pytest.fixture()
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


def _swap(value: Any, repo: str, home: str) -> Any:
    """``value`` with the fixture's /home/dev paths moved to this test's repo and home."""
    if isinstance(value, str):
        return value.replace(FIXTURE_REPO, repo).replace(FIXTURE_HOME, home)
    if isinstance(value, dict):
        return {k: _swap(v, repo, home) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap(v, repo, home) for v in value]
    return value


def _host(name: str, repo: Path | str = FIXTURE_REPO, home: Path | str = FIXTURE_HOME) -> dict[str, Any]:
    """A host fixture (payloads, environment, the hook's working directory) placed in ``repo`` / ``home``."""
    data = json.loads((HOSTS / f"{name}.json").read_text())
    assert data["host"] == name
    return _swap(data, str(repo), str(home))


def _claude(name: str, **overrides: Any) -> dict[str, Any]:
    payload = json.loads((CLAUDE_FIXTURES / name).read_text())["payload"]
    return {**payload, **overrides}


def _repo(tmp_path: Path, slug: str) -> Path:
    _, clones = make_remote_and_clones(tmp_path, ("laptop",))
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", f"https://github.com/acme/{slug}.git")
    commit(repo, "src/app.py", "def app(): ...\n", "feat: app")
    return repo


def _trail(url: str, repo: Path, home: Path) -> list[dict[str, Any]]:
    out = relay(home, url, "trail", "--cwd", str(repo), "--format", "json")
    return json.loads(out.stdout or "{}").get("items") or []


def _wait_for(url: str, repo: Path, home: Path, agent: str, timeout: float = 30) -> list[dict[str, Any]]:
    """The trail items of ``agent`` for ``repo``, waiting for a detached close to post."""
    deadline = time.monotonic() + timeout
    while True:
        items = [i for i in _trail(url, repo, home) if i.get("agent_id") == agent]
        if items or time.monotonic() > deadline:
            return items
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# A recording server: counts what the relay sends
# ---------------------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers a brief and a close like the API; records each request (method, path, agent header, body)."""

    requests: list[dict[str, Any]] = []

    def _record(self, body: dict[str, Any]) -> None:
        agent = self.headers.get("X-Remembra-Agent-Id")
        self.requests.append({"method": self.command, "path": self.path.split("?")[0], "agent": agent, "body": body})

    def _send(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record({})
        self._send({"rendered": "# Remembra brief (recorder)"})

    def do_POST(self) -> None:
        self._record(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
        self._send({"handoff_id": "h1", "project_id": "p", "headline": "ok"})

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def recorder() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    handler = type("Handler", (_Recorder,), {"requests": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler.requests
    finally:
        srv.shutdown()
        srv.server_close()


def _closes(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The close bodies sent, each with the agent header it went with (``_agent``)."""
    return [
        {**r["body"], "_agent": r["agent"]} for r in requests if (r["method"], r["path"]) == ("POST", "/api/v1/session/close")
    ]


def _settle(requests: list[Any], at_least: int, timeout: float = 20.0, quiet: float = 1.5) -> None:
    """Wait for ``at_least`` requests (detached closes post after the hook returns), then a quiet spell."""
    deadline = time.monotonic() + timeout
    while len(requests) < at_least and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(quiet)


# ---------------------------------------------------------------------------
# Detection: each marker, and the Claude Code guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["start", "end"])
@pytest.mark.parametrize("name", ALL_HOSTS)
def test_each_host_is_named_from_its_own_payload_and_environment(name, which, home):
    fx = _host(name, home=home)
    assert hosts.detect_host(CLAUDE, fx["payloads"][which], fx["env"], home) == name


@pytest.mark.parametrize(
    ("name", "payload_extra", "env"),
    [
        ("grok", {"workspaceRoot": FIXTURE_REPO}, {}),
        ("grok", {}, {"GROK_WORKSPACE_ROOT": FIXTURE_REPO}),
        ("cursor", {"cursor_version": "2026.09.26-dd393fe"}, {}),
        ("kimi", {"client_type": "kimi_code_cli"}, {}),
        ("gemini", {}, {"GEMINI_SESSION_ID": "g1"}),
        ("qwen", {}, {"QWEN_CODE_SESSION_ID": "q1"}),
        ("devin", {}, {"DEVIN_PROJECT_DIR": FIXTURE_REPO}),
        ("continue", {}, {"CONTINUE_PROJECT_DIR": FIXTURE_REPO}),
    ],
)
def test_each_marker_alone_names_its_host(name, payload_extra, env, home):
    """A Claude-shaped payload is Claude Code's until one marker says otherwise."""
    claude_shaped = {"session_id": "s1", "cwd": FIXTURE_REPO, "hook_event_name": "SessionEnd", "reason": "other"}
    assert hosts.detect_host(CLAUDE, claude_shaped, {}, home) is None
    assert hosts.detect_host(CLAUDE, {**claude_shaped, **payload_extra}, env, home) == name
    # An empty variable is not a marker (Qwen Code sets several of its own variables empty).
    if env:
        assert hosts.detect_host(CLAUDE, claude_shaped, {k: "" for k in env}, home) is None


def test_a_genuine_claude_code_session_stays_claude_code_whatever_its_environment_holds(home):
    """Qwen Code sets QWEN_CODE_SESSION_ID for its shell tool (and Gemini its own variables): a Claude Code
    session started there has them all, but its transcript is under ~/.claude/projects."""
    every_env_marker = {
        "QWEN_CODE_SESSION_ID": "q1",
        "GEMINI_SESSION_ID": "g1",
        "DEVIN_PROJECT_DIR": "/w",
        "CONTINUE_PROJECT_DIR": "/w",
        "GROK_WORKSPACE_ROOT": "/w",
    }
    transcript = f"{home}/.claude/projects/-home-dev-work-widget/5eba79cf-f4bf-45fe-a76c-c347ec8373da.jsonl"
    for name in ("sessionend-after-rate_limit.json", "stopfailure-rate_limit.json", "stopfailure-billing_error.json"):
        payload = _claude(name, transcript_path=transcript)
        assert hosts.detect_host(CLAUDE, payload, every_env_marker, home) is None, name
        assert hosts.detect_host(CLAUDE, payload, {}, home) is None, name


def test_claude_config_dir_moves_the_guard_with_it(tmp_path, home):
    """The recorded payloads came from a run with CLAUDE_CONFIG_DIR=<tmp>/config: transcripts under it."""
    config = tmp_path / "config"
    payload = _claude(
        "sessionend-after-rate_limit.json",
        transcript_path=f"{config}/projects/-S0-TMP-mock-stopfailure-ratelimit-repo/5eba79cf.jsonl",
    )
    shell = {"GEMINI_SESSION_ID": "g1"}  # Claude Code started from Gemini CLI's shell
    assert hosts.detect_host(CLAUDE, payload, shell, home) == "gemini"  # without the variable: not its own dir
    assert hosts.detect_host(CLAUDE, payload, {**shell, "CLAUDE_CONFIG_DIR": str(config)}, home) is None


def test_a_codex_payload_is_named_by_its_rollout_path(home):
    """Codex sends exactly Claude's fields; its rollout under ~/.codex/sessions names it."""
    for name in ("session_start.json", "session_end.json"):
        payload = _swap(json.loads((CODEX_FIXTURES / name).read_text()), FIXTURE_REPO, str(home))
        assert hosts.detect_host(CLAUDE, payload, {}, home) == "codex"
        assert hosts.detect_host(REGISTRY["codex"], payload, {}, home) is None  # its own hook


@pytest.mark.parametrize("transcript", ["~no-such-user-zz9/x.jsonl", "bad\x00path", "", "   ", None, 42, ["a"]])
def test_odd_transcript_values_never_break_detection(transcript, home):
    payload = {"session_id": "s1", "hook_event_name": "SessionEnd", "transcript_path": transcript, "transcriptPath": transcript}
    assert hosts.detect_host(CLAUDE, payload, {}, home) is None
    assert hosts.detect_host(CLAUDE, payload, {"QWEN_CODE_SESSION_ID": "q"}, home) == "qwen"
    assert hosts.detect_host(CLAUDE, {}, {}, home) is None  # an empty payload (a hook that lost its stdin)


def test_a_hook_run_by_its_own_agent_is_not_foreign(home):
    for name in ("cursor", "kimi", "gemini", "qwen"):
        fx = _host(name, home=home)
        assert hosts.detect_host(REGISTRY[name], fx["payloads"]["end"], fx["env"], home) is None, name
    # Grok also loads ~/.cursor/hooks.json: a Cursor hook in a Grok session is Grok's.
    grok = _host("grok", home=home)
    assert hosts.detect_host(REGISTRY["cursor"], grok["payloads"]["end"], grok["env"], home) == "grok"


# ---------------------------------------------------------------------------
# Brief: another agent's run prints nothing and asks the server nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_HOSTS)
def test_a_brief_another_agent_runs_prints_nothing_and_makes_no_request(name, home, tmp_path):
    repo = _repo(tmp_path, f"brief-{name}")
    fx = _host(name, repo, home)
    with recorder() as (url, requests):
        out = relay(home, url, "brief", *CLAUDE_HOOK, stdin=json.dumps(fx["payloads"]["start"]), env=fx["env"])
        # The same brief as the host's own hook still works: the recorder answers it.
        control = relay(home, url, "brief", "--agent", name, "--cwd", str(repo))
    assert out.returncode == 0 and out.stderr == "", out.stderr
    # Cursor logs a hook with empty stdout as failed: it gets an empty JSON object instead.
    assert out.stdout == ("{}\n" if name == "cursor" else "")
    # Only the control brief reached the server, as that agent.
    assert [(r["method"], r["path"], r["agent"]) for r in requests] == [("GET", "/api/v1/session/brief", name)]
    assert "# Remembra brief (recorder)" in control.stdout
    sessions = home / ".remembra" / "relay" / "sessions"
    assert not list(sessions.glob("brief-*"))  # no "brief delivered" marker for the foreign session
    log = (home / ".remembra" / "relay" / "relay.log").read_text()
    assert f"brief --hook claude-code: run by {name}, nothing to do" in log


# ---------------------------------------------------------------------------
# Close: filed under the agent that ran it, with that agent's cwd
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ROUTED)
def test_a_close_another_agent_runs_is_filed_under_that_agent(name, server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path, f"routed-{name}")
    fx = _host(name, repo, home)
    hook_cwd = Path(fx["hook_cwd"])
    hook_cwd.mkdir(parents=True, exist_ok=True)
    if name == "cursor":
        # Cursor runs the user's Claude hooks in ~/.claude, and its payload has no cwd.
        hook_cwd = home / ".claude"
        hook_cwd.mkdir(exist_ok=True)
        assert "cwd" not in fx["payloads"]["end"] and fx["payloads"]["end"]["workspace_roots"] == [str(repo)]
    end = fx["payloads"]["end"]
    out = relay(home, server, "close", *CLAUDE_HOOK, stdin=json.dumps(end), env=fx["env"], cwd=hook_cwd)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ("{}\n" if name == "cursor" else "")
    items = _wait_for(server, repo, home, name)
    assert len(items) == 1, _trail(server, repo, home)
    item = items[0]
    assert item["session_id"] == end["session_id"] and item["project_id"] == f"routed-{name}"
    assert item["detail"]["end_reason"] == end["reason"]
    assert not [i for i in _trail(server, repo, home) if i["agent_id"] == "claude-code"]
    content = _api(server, "GET", "/timeline", params={"project_id": f"routed-{name}", "memory_type": "handoff"})
    assert any(f"[HANDOFF] {name}" in m["content"] for m in content["memories"])


def test_cursor_close_lands_in_the_workspace_not_in_claude_s_directory(server, home, tmp_path):  # noqa: F811
    """Before routing, Cursor's copy of the Claude hook was read with Claude's mapping: no cwd in the payload,
    so the project came from the hook's working directory, ~/.claude. Now its workspace_roots is read."""
    repo = _repo(tmp_path, "cursor-workspace")
    fx = _host("cursor", repo, home)
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    out = relay(
        home, server, "close", *CLAUDE_HOOK, "--dry-run", stdin=json.dumps(fx["payloads"]["end"]), env=fx["env"], cwd=claude_dir
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["agent_id"] == "cursor" and payload["session_id"] == fx["payloads"]["end"]["session_id"]
    assert Path(payload["project"]["root_path"]).resolve() == repo.resolve()
    assert ".claude" not in json.dumps(payload["project"])
    assert payload["facts"]["facts_source"] == "relay-cli:git"  # a git project, not an agent-declared ~/.claude


@pytest.mark.parametrize("name", DROPPED)
def test_a_close_run_by_an_agent_without_an_adapter_does_nothing(name, home, tmp_path):
    repo = _repo(tmp_path, f"dropped-{name}")
    fx = _host(name, repo, home)
    with recorder() as (url, requests):
        out = relay(home, url, "close", *CLAUDE_HOOK, stdin=json.dumps(fx["payloads"]["end"]), env=fx["env"], cwd=repo)
        _settle(requests, 0, quiet=0.5)
    assert out.returncode == 0 and out.stdout == "" and out.stderr == "", out.stderr
    assert requests == []
    assert not outbox.pending(home)  # nothing queued for later either
    log = (home / ".remembra" / "relay" / "relay.log").read_text()
    assert f"close --hook claude-code: run by {name}, nothing to do" in log


def test_a_grok_session_s_transcript_is_never_parsed_as_claude_jsonl(monkeypatch, capsys, home, tmp_path):
    """Grok's updates.jsonl is an ACP stream. Even one that happens to look like Claude JSONL is not read."""
    repo = _repo(tmp_path, "grok-transcript")
    fx = _host("grok", repo, home)
    end = fx["payloads"]["end"]
    transcript = Path(end["transcriptPath"])
    transcript.parent.mkdir(parents=True)
    t = Transcript(end["sessionId"], repo)
    t.bash("pytest -q", 1, "=== 1 failed in 0.1s ===")
    t.write(transcript)
    assert end["transcript_path"] == str(transcript)

    parsed: list[Any] = []
    sent: list[Any] = []
    monkeypatch.setattr(factlib, "parse_transcript", lambda *a, **k: parsed.append(a))
    monkeypatch.setattr(cli.Context, "request", lambda self, *a, **k: sent.append(a))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_test_key_for_hosts_0123456789")
    for key, value in fx["env"].items():
        monkeypatch.setenv(key, value)
    for payload in (fx["payloads"]["start"], end):
        verb = "brief" if payload is fx["payloads"]["start"] else "close"
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert cli.main([verb, *CLAUDE_HOOK]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert parsed == [] and sent == []


def test_a_grok_subagent_s_end_does_nothing(home, tmp_path):
    repo = _repo(tmp_path, "grok-subagent")
    fx = _host("grok", repo, home)
    sub = fx["payloads"]["subagent_end"]
    assert sub["subagentType"] == "explore"
    with recorder() as (url, requests):
        for hook in (CLAUDE_HOOK, ("--hook", "cursor", "--agent", "cursor")):  # Grok runs both files
            out = relay(home, url, "close", *hook, stdin=json.dumps(sub), env=fx["env"], cwd=repo)
            assert out.returncode == 0, out.stderr
        _settle(requests, 0, quiet=0.5)
    assert requests == []


def test_skip_payload_keys_drop_a_subagent_end_for_an_adapter_that_declares_them(monkeypatch, capsys, home, tmp_path):
    """``AdapterSpec.skip_payload_keys`` (Grok Build's ``subagentType``), on an adapter that runs the close."""
    repo = _repo(tmp_path, "skip-keys")
    gemini = REGISTRY["gemini"]
    skipping = type(gemini)(dataclasses.replace(gemini.spec, skip_payload_keys=("subagentType",)))
    monkeypatch.setitem(REGISTRY, "gemini", skipping)
    fx = _host("gemini", repo, home)
    spawned: list[Any] = []
    monkeypatch.setattr(cli, "spawn_detached_close", lambda args, payload: spawned.append(payload) or True)
    monkeypatch.setenv("HOME", str(home))
    for key, value in fx["env"].items():
        monkeypatch.setenv(key, value)

    def run(payload: dict[str, Any]) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert cli.main(["close", "--hook", "gemini", "--agent", "gemini"]) == 0

    run({**fx["payloads"]["end"], "subagentType": "explore"})
    assert spawned == []
    run(fx["payloads"]["end"])  # the session's own end still closes
    assert len(spawned) == 1
    assert capsys.readouterr().out == ""


def test_a_genuine_claude_code_close_is_still_claude_code_s(server, home, tmp_path):  # noqa: F811
    """The recorded SessionEnd, from a Claude Code started inside Qwen Code's shell: filed as claude-code,
    with its transcript parsed as Claude JSONL."""
    repo = _repo(tmp_path, "genuine-claude")
    session = "5eba79cf-f4bf-45fe-a76c-c347ec8373da"
    transcript = home / ".claude" / "projects" / "-work-genuine-claude" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    t = Transcript(session, repo)
    t.bash("pytest -q tests/test_app.py", 1, "=== 1 failed, 3 passed in 0.2s ===")
    t.write(transcript)
    start = {"session_id": session, "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup",
             "transcript_path": str(transcript)}  # fmt: skip
    qwen_shell = {"QWEN_CODE_SESSION_ID": "d5c75303-7f2a-4b34-a1d3-caabc7e47df9", "QWEN_PROJECT_DIR": str(repo)}
    brief = relay(home, server, "brief", *CLAUDE_HOOK, stdin=json.dumps(start), env=qwen_shell)
    assert brief.returncode == 0 and "# Remembra brief · project genuine-claude · you are claude-code" in brief.stdout
    end = _claude("sessionend-after-rate_limit.json", session_id=session, cwd=str(repo), transcript_path=str(transcript))
    close = relay(home, server, "close", *CLAUDE_HOOK, stdin=json.dumps(end), env=qwen_shell)
    assert close.returncode == 0 and close.stdout == "" and close.stderr == "", close.stderr
    [item] = _trail(server, repo, home)
    assert item["agent_id"] == "claude-code" and item["session_id"] == session
    handoff = _api(server, "GET", "/timeline", params={"project_id": "genuine-claude", "memory_type": "handoff"})
    assert "pytest -q tests/test_app.py" in handoff["memories"][0]["content"]  # read from the Claude transcript


# ---------------------------------------------------------------------------
# C2: one close per session end
# ---------------------------------------------------------------------------


def _gemini_end(repo: Path, home: Path) -> tuple[dict[str, Any], dict[str, str]]:
    fx = _host("gemini", repo, home)
    return fx["payloads"]["end"], fx["env"]


def test_three_gemini_session_ends_make_one_post(home, tmp_path):
    """Gemini CLI 0.61.0 on /quit: SessionEnd three times within ~30 ms, the third orphaned with empty stdin."""
    repo = _repo(tmp_path, "gemini-quit")
    end, env = _gemini_end(repo, home)
    with recorder() as (url, requests):
        for stdin in (json.dumps(end), json.dumps(end), ""):
            out = relay(home, url, "close", "--hook", "gemini", "--agent", "gemini", stdin=stdin, env=env, cwd=repo)
            assert out.returncode == 0 and out.stdout == "", out.stderr
        _settle(requests, 1)
    closes = _closes(requests)
    assert len(closes) == 1, requests
    assert closes[0]["agent_id"] == closes[0]["_agent"] == "gemini" and closes[0]["session_id"] == end["session_id"]
    assert closes[0]["end_reason"] == "exit"
    log = (home / ".remembra" / "relay" / "relay.log").read_text()
    assert "close: dropped a repeat sessionend of gemini session" in log
    assert "close --hook gemini: empty payload (an orphaned end hook), skipped" in log


def test_an_orphaned_empty_gemini_session_end_alone_makes_no_post(home, tmp_path):
    repo = _repo(tmp_path, "gemini-orphan")
    _, env = _gemini_end(repo, home)
    with recorder() as (url, requests):
        out = relay(home, url, "close", "--hook", "gemini", "--agent", "gemini", stdin="", env=env, cwd=repo)
        _settle(requests, 0, quiet=1.0)
    assert out.returncode == 0 and requests == []


def test_claude_stopfailure_then_session_end_make_two_posts(home, tmp_path):
    repo = _repo(tmp_path, "claude-two")
    common = {"session_id": "sess-two", "cwd": str(repo), "transcript_path": str(tmp_path / "none.jsonl")}
    with recorder() as (url, requests):
        for name in ("stopfailure-rate_limit.json", "sessionend-after-rate_limit.json", "sessionend-after-rate_limit.json"):
            out = relay(home, url, "close", *CLAUDE_HOOK, stdin=json.dumps(_claude(name, **common)))
            assert out.returncode == 0, out.stderr
    assert [c["end_reason"] for c in _closes(requests)] == ["rate_limit", "other"]  # the repeated SessionEnd: dropped


def test_one_session_end_run_by_two_agents_copies_makes_one_post(home, tmp_path):
    """Cursor with both claude-code and cursor connected: its own sessionEnd and its copy of Claude's."""
    repo = _repo(tmp_path, "cursor-twice")
    fx = _host("cursor", repo, home)
    (home / ".claude").mkdir()
    end = json.dumps(fx["payloads"]["end"])
    with recorder() as (url, requests):
        own = relay(home, url, "close", "--hook", "cursor", "--agent", "cursor", stdin=end, env=fx["env"], cwd=repo)
        copy_ = relay(home, url, "close", *CLAUDE_HOOK, stdin=end, env=fx["env"], cwd=home / ".claude")
        assert own.stdout == copy_.stdout == "{}\n"
        _settle(requests, 1)
    closes = _closes(requests)
    assert [(c["agent_id"], c["session_id"]) for c in closes] == [("cursor", fx["payloads"]["end"]["session_id"])]


def test_the_close_claim_window_reason_and_transcript(tmp_path):
    home = tmp_path
    args = ("claude-code", "s1", "sessionend", "other")
    assert cli.claim_close(home, *args, 60)
    assert not cli.claim_close(home, *args, 60)  # a repeat within the window
    assert cli.claim_close(home, "claude-code", "s1", "stopfailure", "rate_limit", 60)  # another event and reason
    assert cli.claim_close(home, "claude-code", "s2", "sessionend", "other", 60)  # another session
    assert cli.claim_close(home, "codex", "s1", "sessionend", "other", 60)  # another agent
    # A resumed session that ends again after new turns has a longer transcript: a new close.
    assert cli.claim_close(home, *args, 60, transcript_size=100)
    assert not cli.claim_close(home, *args, 60, transcript_size=100)
    assert cli.claim_close(home, *args, 60, transcript_size=250)
    # Past the window the stale claim is taken over, once.
    marker = cli._close_claim_path(home, *args, "")
    old = time.time() - 120
    os.utime(marker, (old, old))
    assert cli.claim_close(home, *args, 60)
    assert not cli.claim_close(home, *args, 60)
    assert not list(marker.parent.glob("*.stale-*"))  # the renamed stale claim is gone
    assert (marker.stat().st_mode & 0o777) == 0o600


def test_the_close_claim_fails_open_when_its_state_cannot_be_written(tmp_path):
    blocked = tmp_path / "home"
    (blocked / ".remembra").mkdir(parents=True)
    (blocked / ".remembra" / "relay").write_text("not a directory")
    assert cli.claim_close(blocked, "gemini", "s1", "sessionend", "exit", 60)
    assert cli.claim_close(blocked, "gemini", "s1", "sessionend", "exit", 60)  # a lost handoff is worse than two


def _claim_in_child(home: str, start: Any, results: Any) -> None:
    start.wait(10)
    results.put(cli.claim_close(Path(home), "gemini", "race", "sessionend", "exit", 60))


def test_racing_closes_of_one_session_end_elect_exactly_one(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_claim_in_child, args=(str(tmp_path), start, results)) for _ in range(6)]
    for proc in procs:
        proc.start()
    start.set()
    outcomes = [results.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(30)
    assert sorted(outcomes) == [False] * 5 + [True]


def test_three_gemini_fires_spawn_one_detached_close(monkeypatch, home, tmp_path):
    """The same, in-process: the claim is taken before the detached child starts, so only one starts."""
    repo = _repo(tmp_path, "gemini-spawn")
    end, env = _gemini_end(repo, home)
    spawned: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "spawn_detached_close", lambda args, payload: spawned.append(copy.deepcopy(payload)) or True)
    monkeypatch.setenv("HOME", str(home))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for stdin in (json.dumps(end), json.dumps(end), ""):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert cli.main(["close", "--hook", "gemini", "--agent", "gemini"]) == 0
    assert spawned == [end]


def test_the_detached_child_is_not_stopped_by_its_parent_s_claim(server, home, tmp_path):  # noqa: F811
    """The parent claims, then the child (--foreground) runs the close: it must not see the claim as a repeat."""
    repo = _repo(tmp_path, "gemini-child")
    end, env = _gemini_end(repo, home)
    out = relay(home, server, "close", "--hook", "gemini", "--agent", "gemini", stdin=json.dumps(end), env=env, cwd=repo)
    assert out.returncode == 0, out.stderr
    items = _wait_for(server, repo, home, "gemini")
    assert len(items) == 1 and items[0]["detail"]["end_reason"] == "exit"
    claims = list((home / ".remembra" / "relay" / "sessions").glob("close-*.json"))
    assert len(claims) == 1


# ---------------------------------------------------------------------------
# Relay hooks an import copied into another agent's config
# ---------------------------------------------------------------------------

GROK_IMPORTED = """model = "grok-build"

[[hooks.SessionStart]]
hooks = [{ type = "command", command = "/opt/bin/remembra-relay brief --hook claude-code --agent claude-code", timeout = 15 }]

[[hooks.SessionEnd]]
hooks = [{ type = "command", command = "/opt/bin/remembra-relay close --hook claude-code --agent claude-code", timeout = 15 }]

[[hooks.PreToolUse]]
matcher = "Bash"
hooks = [{ type = "command", command = "./guard.sh" }]
"""


def test_copied_relay_hooks_are_found_in_the_files_imports_write(home):
    grok = home / ".grok" / "config.toml"
    grok.parent.mkdir()
    grok.write_text(GROK_IMPORTED)
    gemini = home / ".gemini" / "settings.json"
    gemini.parent.mkdir()
    own = "/opt/bin/remembra-relay close --hook gemini --agent gemini"
    migrated = "/opt/bin/remembra-relay close --hook claude-code --agent claude-code"
    gemini.write_text(
        "// migrated by gemini hooks migrate\n"
        + json.dumps(
            {
                "hooks": {
                    "SessionEnd": [{"hooks": [{"type": "command", "command": own}]}],
                    "PreCompress": [{"hooks": [{"type": "command", "command": migrated}]}],
                }
            }
        )
    )
    kimi = home / ".kimi-code" / "config.toml"
    kimi.parent.mkdir()
    kimi.write_text('[[hooks]]\nevent = "SessionEnd"\ncommand = "/opt/bin/remembra-relay close --hook kimi --agent kimi"\n')
    (home / ".cursor").mkdir()
    (home / ".cursor" / "hooks.json").write_text("{not json")  # unreadable files are skipped

    found = {c.host: c for c in hosts.copied_relay_hooks(home, REGISTRY)}
    assert set(found) == {"grok", "gemini"}  # Kimi Code running --hook kimi hooks is its own agent's
    assert found["grok"].path == grok and found["grok"].hooks == {"claude-code": 2} and found["grok"].total == 2
    assert found["gemini"].path == gemini and found["gemini"].hooks == {"claude-code": 1}


def test_connect_and_disconnect_point_out_copied_hooks(home):
    grok = home / ".grok" / "config.toml"
    grok.parent.mkdir()
    grok.write_text(GROK_IMPORTED)
    (home / ".claude").mkdir()
    out = relay(home, "http://x", "connect", "--agent", "claude-code", "--relay-command", "/opt/bin/remembra-relay")
    assert out.returncode == 0, out.stderr
    assert (
        f"[copied hooks] {grok} holds 2 relay hook(s) written for another agent (2 claude-code, copied by Grok Build's"
        " /import-claude)." in out.stdout
    )
    assert "grok runs them there: the relay sees that it is grok and does nothing, never under the agent" in out.stdout
    assert grok.read_text() == GROK_IMPORTED  # reported, never edited
    gone = relay(home, "http://x", "disconnect")
    assert f"[copied hooks] {grok}" in gone.stdout
    grok.write_text('model = "grok-build"\n')
    clean = relay(home, "http://x", "connect", "--agent", "claude-code", "--relay-command", "/opt/bin/remembra-relay")
    assert "[copied hooks]" not in clean.stdout


def test_a_host_file_moved_by_its_variable_is_checked_there(monkeypatch, tmp_path):
    home = tmp_path / "home"
    moved = tmp_path / "grok-home"
    moved.mkdir(parents=True)
    (moved / "config.toml").write_text(GROK_IMPORTED)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("GROK_HOME", str(moved))
    assert [c.path for c in hosts.copied_relay_hooks(home, REGISTRY)] == [moved / "config.toml"]
    assert hosts.copied_relay_hooks(tmp_path / "other-home", REGISTRY) == []  # not the real home: never followed


# ---------------------------------------------------------------------------
# The subprocess sees what an agent's hook would see
# ---------------------------------------------------------------------------


def test_a_routed_hook_runs_with_nothing_but_the_hosts_environment(home, tmp_path):
    """No inherited variables: exactly the recorded environment plus what a hook needs to run the CLI."""
    repo = _repo(tmp_path, "bare-env")
    fx = _host("qwen", repo, home)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC, **GIT_ENV, **fx["env"]}
    with recorder() as (url, requests):
        env["REMEMBRA_URL"], env["REMEMBRA_API_KEY"] = url, "rem_test_key_for_hosts_0123456789"
        proc = subprocess.run(
            [sys.executable, "-m", "remembra.relay.cli", "close", *CLAUDE_HOOK],
            input=json.dumps(fx["payloads"]["end"]),
            capture_output=True,
            text=True,
            cwd=str(repo),
            env=env,
            timeout=60,
        )
        _settle(requests, 1)
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    [close] = _closes(requests)
    assert close["agent_id"] == close["_agent"] == "qwen" and close["end_reason"] == "prompt_input_exit"
    assert Path(close["project"]["root_path"]).resolve() == repo.resolve()
