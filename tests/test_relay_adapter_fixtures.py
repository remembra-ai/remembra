"""Adapters replayed against payload fixtures (tests/fixtures/relay/, see its README).

Codex fixtures are recorded from a live run (tests/test_relay_codex_live.py);
Gemini CLI, Qwen Code and Cursor fixtures are written from each tool's hook
docs. Also here, against the local test server:

- ``brief --once`` (Codex's UserPromptSubmit hook) delivers a brief once per
  session, and a resumed Codex session does not get a second copy;
- ``close`` for agents that do not wait for the end hook (Gemini CLI, Qwen
  Code, Cursor, Codex) detaches: the handoff arrives when the agent is killed
  100 ms after starting the hook, and when the hook's process group is killed
  after it returns;
- a Codex rollout that ends on the usage limit becomes ``ended: usage_limit``.

Docs: https://developers.openai.com/codex/hooks, https://geminicli.com/docs/hooks/reference/,
https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/, https://cursor.com/docs/agent/hooks
(all accessed 2026-09-25).
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.relay import facts as factlib
from remembra.relay.adapters import REGISTRY
from remembra.relay.cli import main as relay_main
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones
from tests.test_relay_cli_e2e import SRC, _api, relay, server  # noqa: F401  (server is a fixture)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay"
CODEX = FIXTURES / "codex"
FIXTURE_REPO = "/home/dev/work/widget"
DOCS = Path(__file__).resolve().parents[1] / "docs" / "guides" / "relay.md"


@pytest.fixture()
def home(tmp_path):
    """HOME for the relay CLI in these tests (no real config is read)."""
    path = tmp_path / "home"
    path.mkdir()
    return path


def _load(agent: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / agent / name).read_text())


# ---------------------------------------------------------------------------
# Codex: recorded payloads and rollouts
# ---------------------------------------------------------------------------


def test_codex_fixtures_are_a_recorded_run():
    recorded = json.loads((CODEX / "RECORDED.json").read_text())
    assert recorded["codex_cli"] in REGISTRY["codex"].spec.notes
    start, end = _load("codex", "session_start.json"), _load("codex", "session_end.json")
    assert start["session_id"] == end["session_id"]
    assert start["transcript_path"] == end["transcript_path"]


@pytest.mark.parametrize(
    ("name", "event", "reason"),
    [
        ("session_start.json", "SessionStart", None),
        ("user_prompt_submit.json", "UserPromptSubmit", None),
        ("session_end.json", "SessionEnd", "other"),
        ("session_start_resume.json", "SessionStart", None),
    ],
)
def test_codex_payloads_map_to_session_fields(name, event, reason):
    payload = _load("codex", name)
    assert payload["hook_event_name"] == event
    fields = REGISTRY["codex"].spec.payload.extract(payload, environ={})
    assert fields == {
        "session_id": payload["session_id"],
        "cwd": FIXTURE_REPO,
        "transcript": payload["transcript_path"],
        "reason": reason,
    }


def test_codex_rollout_replay_gives_commands_exit_codes_and_a_test_verdict():
    rollout = CODEX / "rollout_round_trip.jsonl"
    assert factlib.detect_transcript_format(rollout) == factlib.CODEX_ROLLOUT
    facts = factlib.parse_codex_rollout(rollout, root=FIXTURE_REPO)
    assert facts.session_id == _load("codex", "session_start.json")["session_id"]
    assert facts.started_at is not None
    pytest_cmd = "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider"
    assert facts.commands == [{"cmd": pytest_cmd, "exit_code": 1}, {"cmd": "sh scripts/build.sh", "exit_code": 127}]
    assert facts.tests == [{"cmd": pytest_cmd, "passed": False, "summary": facts.tests[0]["summary"]}]
    assert re.fullmatch(r"1 failed in \d+\.\d+s", facts.tests[0]["summary"])
    assert len(facts.errors) == 1 and facts.errors[0].startswith("`sh scripts/build.sh` exited 127: ")
    assert "No such file or directory" in facts.errors[0]
    assert facts.usage_limit is None and facts.todos_open == [] and facts.files == []


def test_codex_rollout_commands_outside_the_repo_are_dropped():
    facts = factlib.parse_codex_rollout(CODEX / "rollout_round_trip.jsonl", root="/home/dev/work/other-repo")
    assert facts.commands == [] and facts.tests == [] and facts.errors == []


def test_codex_usage_limit_is_read_from_the_rollout_tail():
    """The captured Codex limit message (a 429 usage_limit_reached, rendered by Codex itself)."""
    facts = factlib.parse_codex_rollout(CODEX / "rollout_usage_limit.jsonl", root=FIXTURE_REPO)
    assert facts.usage_limit is not None
    assert facts.usage_limit.replace("’", "'").startswith("You've hit your usage limit.")
    assert "chatgpt.com/codex/settings/usage" in facts.usage_limit
    raw = [json.loads(line) for line in (CODEX / "rollout_usage_limit.jsonl").read_text().splitlines()]
    done = [e["payload"] for e in raw if e["type"] == "event_msg" and e["payload"].get("type") == "task_complete"]
    assert done[-1]["error"]["codex_error_info"] == factlib.CODEX_USAGE_LIMIT_INFO


def _rollout(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "rollout.jsonl"
    meta = {"type": "session_meta", "payload": {"id": "s-1", "cwd": "/repo", "timestamp": "2026-09-25T10:00:00Z"}}
    path.write_text("\n".join(json.dumps(r) for r in [meta, *records]) + "\n")
    return path


def _event(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "event_msg", "payload": payload}


def test_codex_limit_is_cleared_by_a_later_turn_and_found_by_its_text(tmp_path):
    limited = _event({"type": "task_complete", "error": {"message": "You've hit your usage limit. Try again at 4 PM."}})
    assert factlib.parse_codex_rollout(_rollout(tmp_path, [limited])).usage_limit.startswith("You've hit")
    recovered = [limited, _event({"type": "task_started"}), _event({"type": "task_complete", "last_agent_message": "ok"})]
    assert factlib.parse_codex_rollout(_rollout(tmp_path, recovered)).usage_limit is None
    overloaded = {"message": "stream disconnected", "codex_error_info": "server_overloaded"}
    other = _event({"type": "task_complete", "error": overloaded})
    assert factlib.parse_codex_rollout(_rollout(tmp_path, [other])).usage_limit is None
    # Older versions: an `error` event, then a task_complete without an error field. The stop stands.
    legacy = [
        _event({"type": "task_started"}),
        _event({"type": "error", "message": "You’ve hit your usage limit."}),
        _event({"type": "task_complete", "last_agent_message": None}),
    ]
    assert factlib.parse_codex_rollout(_rollout(tmp_path, legacy)).usage_limit == "You’ve hit your usage limit."


def test_codex_older_record_shapes(tmp_path):
    """Not recorded here: the shapes older Codex versions wrote (function_call + JSON output
    with metadata.exit_code, exec_command_end events, update_plan). Parsed so an older
    rollout still yields facts; events win over the function_call fallback for the same call."""
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": ["bash", "-lc", "npm test"], "workdir": "/repo"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"output": "Tests: 2 failed, 5 passed", "metadata": {"exit_code": 1}}),
            },
        },
        _event(
            {
                "type": "exec_command_end",
                "call_id": "c2",
                "command": ["zsh", "-lc", "make build"],
                "cwd": "/repo",
                "exit_code": 2,
                "aggregated_output": "make: *** [build] Error 2",
            }
        ),
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c3",
                "arguments": json.dumps({"cmd": "git status"}),
            },
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c3", "output": "no exit code here"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "update_plan",
                "call_id": "c4",
                "arguments": json.dumps(
                    {"plan": [{"step": "fix totals", "status": "in_progress"}, {"step": "write tests", "status": "completed"}]}
                ),
            },
        },
        _event(
            {
                "type": "item_completed",
                "item": {"type": "FileChange", "status": "completed", "changes": {"/repo/src/totals.py": {"type": "update"}}},
            }
        ),
        _event({"type": "item_completed", "item": {"type": "FileChange", "status": "declined", "changes": {"/repo/x.py": {}}}}),
    ]
    facts = factlib.parse_codex_rollout(_rollout(tmp_path, records), root="/repo")
    assert facts.commands == [{"cmd": "npm test", "exit_code": 1}, {"cmd": "make build", "exit_code": 2}]
    assert facts.tests == [{"cmd": "npm test", "passed": False, "summary": "Tests: 2 failed, 5 passed"}]
    assert facts.errors == ["`make build` exited 2: make: *** [build] Error 2"]
    assert facts.todos_open == ["fix totals"]
    assert facts.files == ["/repo/src/totals.py"]


def test_transcript_format_detection(tmp_path):
    claude = Transcript("s", tmp_path).write(tmp_path / "claude.jsonl")
    assert factlib.detect_transcript_format(claude) == factlib.CLAUDE_JSONL
    assert factlib.detect_transcript_format(CODEX / "rollout_usage_limit.jsonl") == factlib.CODEX_ROLLOUT
    (tmp_path / "empty.jsonl").write_text("\n\n")
    assert factlib.detect_transcript_format(tmp_path / "empty.jsonl") is None
    (tmp_path / "junk.jsonl").write_text("not json\n")
    assert factlib.detect_transcript_format(tmp_path / "junk.jsonl") is None
    assert factlib.detect_transcript_format(tmp_path / "missing.jsonl") is None
    with pytest.raises(ValueError):
        factlib.parse_transcript(claude, "gemini-json")


def _repo(tmp_path: Path) -> Path:
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", f"https://github.com/acme/{tmp_path.name[-20:].lower()}.git")
    return repo


@pytest.fixture()
def dry_close(home, monkeypatch, capsys):
    """``close --hook X --dry-run`` in process, with the payload on stdin; returns the request body."""
    for key, value in {"HOME": str(home), "REMEMBRA_URL": "http://127.0.0.1:9", "REMEMBRA_API_KEY": "rem_x", **GIT_ENV}.items():
        monkeypatch.setenv(key, value)

    def run(hook: str, payload: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert relay_main(["close", "--hook", hook, "--agent", hook, "--dry-run"]) == 0
        out = capsys.readouterr().out
        return dict(json.loads(out[out.index("{") :]))

    return run


def test_codex_close_routes_a_usage_limit_stop_to_the_quota_path(tmp_path, dry_close):
    """The recorded limit rollout, closed through the codex hook: end_reason usage_limit, reason first."""
    repo = _repo(tmp_path)
    rollout = tmp_path / "rollout.jsonl"
    shutil.copy(CODEX / "rollout_usage_limit.jsonl", rollout)
    payload = {**_load("codex", "session_end.json"), "cwd": str(repo), "transcript_path": str(rollout)}
    body = dry_close("codex", payload)
    assert body["end_reason"] == "usage_limit"  # the transcript beats the hook's uninformative "other"
    assert body["facts"]["errors"][0].replace("’", "'").startswith("Stopped on a usage limit: You've hit your usage limit.")
    assert body["facts"]["facts_source"] == "relay-cli:git+transcript"
    assert body["agent_id"] == "codex" and body["session_id"] == payload["session_id"]


def test_codex_close_parses_the_round_trip_rollout(tmp_path, dry_close):
    repo = _repo(tmp_path)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text((CODEX / "rollout_round_trip.jsonl").read_text().replace(FIXTURE_REPO, str(repo)))
    payload = {**_load("codex", "session_end.json"), "cwd": str(repo), "transcript_path": str(rollout)}
    body = dry_close("codex", payload)
    assert body["end_reason"] == "other"
    assert [c["exit_code"] for c in body["facts"]["commands"]] == [1, 127]
    assert body["facts"]["tests"][0]["passed"] is False


def test_gemini_transcript_is_not_parsed_but_close_still_works(tmp_path, dry_close):
    repo = _repo(tmp_path)
    chat = tmp_path / "session.json"
    chat.write_text('{"messages": []}')
    payload = {**_load("gemini", "session_end.json"), "cwd": str(repo), "transcript_path": str(chat)}
    body = dry_close("gemini", payload)
    assert body["facts"]["facts_source"] == "relay-cli:git" and body["end_reason"] == "exit"
    assert "commands" not in body["facts"]


# ---------------------------------------------------------------------------
# Gemini CLI, Qwen Code, Cursor: doc-derived payloads, config shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "reason"),
    [("gemini", "exit"), ("qwen", "prompt_input_exit"), ("cursor", "user_close")],
)
def test_doc_payloads_map_to_session_fields(agent, reason):
    spec = REGISTRY[agent].spec
    start, end = _load(agent, "session_start.json"), _load(agent, "session_end.json")
    assert start["hook_event_name"] == spec.start_event and end["hook_event_name"] == spec.end_event
    for payload, want_reason in ((start, None), (end, reason)):
        fields = spec.payload.extract(payload, environ={})
        assert fields == {
            "session_id": payload["session_id"],
            "cwd": FIXTURE_REPO,
            "transcript": payload["transcript_path"],
            "reason": want_reason,
        }


def test_env_fallbacks_when_the_payload_lacks_fields():
    assert REGISTRY["gemini"].spec.payload.extract({}, environ={"GEMINI_SESSION_ID": "g1", "GEMINI_PROJECT_DIR": "/p"}) == {
        "session_id": "g1",
        "cwd": "/p",
        "transcript": None,
        "reason": None,
    }
    assert REGISTRY["qwen"].spec.payload.extract({"session_id": "q"}, environ={"QWEN_PROJECT_DIR": "/q"})["cwd"] == "/q"
    cursor = REGISTRY["cursor"].spec.payload.extract({"conversation_id": "c"}, environ={"CURSOR_PROJECT_DIR": "/c"})
    assert cursor["session_id"] == "c" and cursor["cwd"] == "/c"


def test_timeouts_are_written_in_each_agents_unit(tmp_path):
    def timeouts(agent: str) -> dict[str, Any]:
        after = json.loads(REGISTRY[agent].plan(tmp_path / agent, "/bin/relay").after)["hooks"]
        if agent == "cursor":
            return {event: entries[0]["timeout"] for event, entries in after.items()}
        return {event: groups[0]["hooks"][0]["timeout"] for event, groups in after.items()}

    assert timeouts("gemini") == {"SessionStart": 15000, "SessionEnd": 15000}  # milliseconds: 15 would be 15 ms
    assert timeouts("qwen") == {"SessionStart": 15, "SessionEnd": 15}  # seconds (>= 1000 would be read as ms)
    assert timeouts("cursor") == {"sessionStart": 15, "sessionEnd": 15}
    assert timeouts("codex") == {"SessionStart": 15, "UserPromptSubmit": 15, "SessionEnd": 3}  # SessionEnd max is 3 s
    assert timeouts("claude-code") == {"SessionStart": 15, "SessionEnd": 15}


def test_cursor_config_keeps_other_hooks_and_is_idempotent(tmp_path):
    adapter = REGISTRY["cursor"]
    path = adapter.spec.config_path(tmp_path)
    path.parent.mkdir(parents=True)
    theirs = {"sessionStart": [{"command": "./audit.sh"}], "stop": [{"command": "x"}]}
    path.write_text(json.dumps({"version": 1, "hooks": theirs}))
    change = adapter.plan(tmp_path, "/bin/relay")
    data = json.loads(change.after)
    assert data["hooks"]["sessionStart"] == [
        {"command": "./audit.sh"},
        {"command": "/bin/relay brief --hook cursor --agent cursor", "timeout": 15},
    ]
    assert data["hooks"]["stop"] == [{"command": "x"}]
    path.write_text(change.after)
    assert not adapter.plan(tmp_path, "/bin/relay").changed
    # An older entry without a timeout is updated, not duplicated.
    data["hooks"]["sessionEnd"] = [{"command": "/bin/relay close --hook cursor --agent cursor"}]
    path.write_text(json.dumps(data))
    again = json.loads(adapter.plan(tmp_path, "/bin/relay").after)
    assert again["hooks"]["sessionEnd"] == [{"command": "/bin/relay close --hook cursor --agent cursor", "timeout": 15}]


def test_adapters_that_detach_close_are_the_ones_whose_agent_does_not_wait():
    assert {name for name, a in REGISTRY.items() if a.spec.detach_close} == {"codex", "gemini", "qwen", "cursor"}
    assert {name for name, a in REGISTRY.items() if a.spec.verified} == {"claude-code", "codex"}


# ---------------------------------------------------------------------------
# Against the local server: brief --once, detached close
# ---------------------------------------------------------------------------


def _brief(home: Path, url: str, payload: dict[str, Any], *extra: str) -> str:
    out = relay(home, url, "brief", "--hook", "codex", "--agent", "codex", *extra, stdin=json.dumps(payload))
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_codex_brief_is_delivered_once_per_session(server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path)
    prompt = {**_load("codex", "user_prompt_submit.json"), "cwd": str(repo), "session_id": "restored-1"}
    first = _brief(home, server, prompt, "--once")
    assert first.startswith("# Remembra brief · project ") and "you are codex" in first
    assert _brief(home, server, prompt, "--once") == ""  # the next prompt in the same session
    assert _brief(home, server, {**prompt, "session_id": "restored-2"}, "--once") != ""  # another session

    start = {**_load("codex", "session_start.json"), "cwd": str(repo), "session_id": "fresh-1"}
    assert _brief(home, server, start) != ""  # a new session always gets it
    assert _brief(home, server, {**prompt, "session_id": "fresh-1"}, "--once") == ""  # ...and only once
    resumed = {**_load("codex", "session_start_resume.json"), "cwd": str(repo), "session_id": "fresh-1"}
    assert _brief(home, server, resumed) == ""  # resume: the restored context already holds it
    compacted = {**start, "source": "compact"}
    assert _brief(home, server, compacted) != ""  # compaction may drop it: deliver again
    assert _brief(home, server, {k: v for k, v in prompt.items() if k != "session_id"}, "--once") == ""  # no id: no guess


def _wait_for_handoff(home: Path, url: str, repo: Path, agent: str, timeout: float = 30) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        out = relay(home, url, "trail", "--cwd", str(repo), "--format", "json")
        items = [i for i in json.loads(out.stdout or "{}").get("items") or [] if i.get("agent_id") == agent]
        if items or time.monotonic() > deadline:
            return items
        time.sleep(0.25)


def _hook_env(home: Path, url: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": SRC,
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": "rem_test_key_for_e2e",
        **GIT_ENV,
    }


class SlowProxy:
    """Forwards to the test server after ``delay`` seconds, so a close is still in flight
    when the test kills processes, and drops a request whose caller has gone. A close that
    did not detach would be killed with its caller and never arrive."""

    def __init__(self, upstream: str, delay: float) -> None:
        owner = self
        self.upstream, self.delay = upstream, delay

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                time.sleep(owner.delay)
                if self._client_gone():  # a killed caller's request is dropped, as a real server would
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
                res = httpx.request(self.command, owner.upstream + self.path, content=body, headers=headers, timeout=30)
                self.send_response(res.status_code)
                self.send_header("content-type", res.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(res.content)))
                self.end_headers()
                self.wfile.write(res.content)

            def _client_gone(self) -> bool:
                self.connection.setblocking(False)
                try:
                    return self.connection.recv(1, socket.MSG_PEEK) == b""
                except BlockingIOError:
                    return False
                except OSError:
                    return True
                finally:
                    self.connection.setblocking(True)

            do_GET = do_POST = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


AGENT_SIM = textwrap.dedent(
    """
    import json, subprocess, sys, time
    hook = subprocess.Popen(json.loads(sys.argv[1]), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hook.stdin.write(sys.argv[2].encode()); hook.stdin.close()
    print(hook.pid, time.monotonic(), flush=True)
    time.sleep(120)
    """
)


def _gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_gemini_close_survives_the_cli_being_killed_100ms_after_the_hook_starts(server, home, tmp_path):  # noqa: F811
    """Gemini CLI does not wait for SessionEnd ("the CLI will not wait"): it may exit, and its
    process group may be torn down, while the hook is still posting.

    The CLI (a stand-in process that runs the hook the way a CLI does, pipes and all) is
    killed 100 ms after it starts the hook. Then everything left in the CLI's process group
    is killed while the handoff POST is still held by a slow proxy. The handoff arrives
    because the posting process runs in its own session.
    """
    repo = _repo(tmp_path)
    commit(repo, "totals.py", "def total(): ...\n", "feat: totals")
    payload = {**_load("gemini", "session_end.json"), "cwd": str(repo), "transcript_path": None}
    command = [sys.executable, "-m", "remembra.relay.cli", "close", "--hook", "gemini", "--agent", "gemini"]
    proxy = SlowProxy(server, delay=3.0)
    try:
        sim = subprocess.Popen(
            [sys.executable, "-c", AGENT_SIM, json.dumps(command), json.dumps(payload)],
            stdout=subprocess.PIPE,
            text=True,
            env=_hook_env(home, proxy.url),
            cwd=str(repo),
            start_new_session=True,  # the CLI's own process group, like a terminal job
        )
        assert sim.stdout is not None
        hook_pid, started = sim.stdout.readline().split()
        time.sleep(max(0.0, float(started) + 0.1 - time.monotonic()))
        sim.kill()  # the CLI dies 100 ms after starting the hook; the hook's pipes close with it
        sim.wait(10)
        # Give the hook up to 1.5 s to hand off and return (a close that posts itself would still
        # be waiting on the 3 s proxy), then kill the rest of the CLI's group either way.
        _gone(int(hook_pid), 1.5)
        with contextlib.suppress(ProcessLookupError):  # empty group: nothing of ours was left in it
            os.killpg(sim.pid, signal.SIGKILL)  # tear down whatever is left in the CLI's group
        items = _wait_for_handoff(home, server, repo, "gemini")
    finally:
        proxy.close()
    assert len(items) == 1, "the handoff did not arrive"
    content = _api(server, "GET", "/timeline", params={"project_id": items[0]["project_id"], "memory_type": "handoff"})
    assert any("[HANDOFF] gemini" in m["content"] and "ended: exit" in m["content"] for m in content["memories"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_detached_close_returns_before_the_post_and_survives_its_group_being_killed(server, home, tmp_path):  # noqa: F811
    """Codex kills a SessionEnd hook after 1 s by default (3 s at most). The hook must return
    well before the POST completes, and the POST must outlive the hook's process group."""
    repo = _repo(tmp_path)
    commit(repo, "totals.py", "def total(): ...\n", "feat: totals")
    payload = {**_load("codex", "session_end.json"), "cwd": str(repo), "transcript_path": None}
    proxy = SlowProxy(server, delay=4.0)
    try:
        started = time.monotonic()
        hook = subprocess.Popen(
            [sys.executable, "-m", "remembra.relay.cli", "close", "--hook", "codex", "--agent", "codex"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_hook_env(home, proxy.url),
            cwd=str(repo),
            start_new_session=True,
        )
        out, err = hook.communicate(json.dumps(payload).encode(), timeout=30)
        elapsed = time.monotonic() - started
        assert hook.returncode == 0 and out == b"" and err == b"", err
        assert elapsed < 3.0, f"the hook took {elapsed:.2f}s; Codex allows SessionEnd at most 3 s"
        assert not _wait_for_handoff(home, server, repo, "codex", timeout=0), "posted before the hook returned"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(hook.pid, signal.SIGKILL)  # everything left in the hook's group
        items = _wait_for_handoff(home, server, repo, "codex")
    finally:
        proxy.close()
    log = home / ".remembra" / "relay" / "last-detached-close.log"
    assert len(items) == 1, log.read_text() if log.exists() else "no log"
    assert log.exists() and (log.stat().st_mode & 0o777) == 0o600


def test_dry_run_and_foreground_close_do_not_detach(server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path)
    payload = json.dumps({**_load("codex", "session_end.json"), "cwd": str(repo), "transcript_path": None})
    dry = relay(home, server, "close", "--hook", "codex", "--agent", "codex", "--dry-run", stdin=payload)
    assert dry.returncode == 0 and json.loads(dry.stdout)["agent_id"] == "codex"
    fg = relay(home, server, "close", "--hook", "codex", "--agent", "codex", "--foreground", stdin=payload)
    assert fg.returncode == 0 and fg.stderr == ""
    assert _wait_for_handoff(home, server, repo, "codex", timeout=1)  # posted before the command returned


# ---------------------------------------------------------------------------
# Docs: an adapter is "verified" in the guide only when it is verified in code
# ---------------------------------------------------------------------------


def test_relay_guide_marks_verified_exactly_the_verified_adapters():
    """docs/guides/relay.md shows "verified" for an adapter only when the code says so, and the
    code says so only for Claude Code (the original verification) or an adapter with a recorded run."""
    shown = {
        "Claude Code": "claude-code",
        "Codex CLI": "codex",
        "Cursor IDE": "cursor",
        "Gemini CLI": "gemini",
        "Qwen Code": "qwen",
        "Kimi Code": "kimi",
    }
    rows = {}
    for line in DOCS.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] in shown:
            rows[cells[0]] = cells[2]
    assert set(rows) == set(shown) and set(shown.values()) == set(REGISTRY), rows
    for label, name in shown.items():
        verified = rows[label].split(" ")[0] == "verified"
        assert verified == REGISTRY[name].spec.verified, (label, rows[label])
        if verified and name != "claude-code":
            assert (FIXTURES / name / "RECORDED.json").exists(), name
