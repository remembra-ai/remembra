"""F36: a session whose work left no git or file trace (browser, inbox, research, read-only checks)
ended with "nothing to hand off", in a folder without a repository and in a clean checkout alike.

The close now reads how much the session did from its transcript (prompts, tool calls by name, how
long) and, when nothing else would be handed off, says so in a relay-written note. It never sends
what was said: the raw transcript stays on the machine.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.relay import cli, outbox
from remembra.relay import facts as factlib
from remembra.relay.crew import crewd as crewd_mod
from remembra.relay.handoff import build_sections, render_handoff
from tests.relay_fixtures import Transcript, commit, git, make_remote_and_clones

SECRET_PROMPT = "Clean up the Dolphy Technology address listings on every directory"
SECRET_REPLY = "I updated the Bing Places listing and paused Google pending ownership."


class Desktop(Transcript):
    """A Claude desktop transcript: prompts, assistant text, MCP tool calls, and the ``api-request-blob``
    records that repeat every message (no timestamp, the same ``tool_use`` ids again)."""

    def __init__(self, session_id: str, cwd: Path, prompt: str = SECRET_PROMPT) -> None:
        super().__init__(session_id, cwd)
        self.lines.clear()
        self.minute = 0
        self.prompt(prompt)

    def _tick(self) -> None:
        self.minute += 1
        self.ts = f"2026-11-07T22:{self.minute:02d}:00.000Z"

    def prompt(self, text: str, **extra: Any) -> None:
        self._tick()
        self.lines.append(self._base("user", {"role": "user", "content": text}, **extra))

    def say(self, text: str) -> None:
        self._tick()
        self.lines.append(self._base("assistant", {"role": "assistant", "content": [{"type": "text", "text": text}]}))

    def tool(self, name: str, tool_input: dict[str, Any], result: Any, is_error: bool = False, tur: Any = None) -> None:
        self._tick()
        super().tool(name, tool_input, result, is_error=is_error, tur=tur)
        use = self.lines[-2]["message"]
        # the desktop app's duplicate of the assistant message
        self.lines.append({"type": "api-request-blob", "hash": f"h{self.n}", "sessionId": self.session_id, "message": use})

    def browse(self, times: int) -> None:
        for n in range(times):
            self.tool("mcp__Claude_Browser__navigate", {"url": f"https://example.test/{n}"}, "ok")


class Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        with self.lock:
            self.requests.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"handoff_id": "h-1", "project_id": "notes", "headline": "ok"})

    def closes(self) -> list[dict[str, Any]]:
        return [body for method, path, body in self.requests if (method, path) == ("POST", "/api/v1/session/close")]


@pytest.fixture()
def env(monkeypatch, tmp_path) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "clawbot"  # not a git repository
    folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_activity_test")
    monkeypatch.setenv("REMEMBRA_URL", "http://relay.test")
    for var in ("REMEMBRA_AGENT_ID", "REMEMBRA_PROJECT", "REMEMBRA_RELAY_PROJECT", "REMEMBRA_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    recorder = Recorder()
    monkeypatch.setattr(
        cli.Context,
        "client",
        lambda self, config=None, agent=None, budget=None: httpx.Client(
            base_url="http://relay.test", transport=httpx.MockTransport(recorder.handle)
        ),
    )
    return {"home": home, "folder": folder, "http": recorder, "tmp": tmp_path}


def _hook_close(env: dict[str, Any], monkeypatch, capsys, transcript: Path, session: str, cwd: Path, hook: str = "claude-code"):
    payload = {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(cwd),
        "hook_event_name": "SessionEnd",
        "reason": "other",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    argv = ["close", "--hook", hook, "--agent", hook]
    if hook == "codex":
        argv.append("--foreground")  # Codex detaches; this is the detached child
    assert cli.main(argv) == 0
    capsys.readouterr()
    path = outbox.log_path(env["home"])
    return path.read_text() if path.exists() else ""


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def test_the_parser_counts_prompts_and_each_tool_call_once(tmp_path):
    t = Desktop("s-count", tmp_path)
    t.browse(4)
    t.tool("WebSearch", {"query": "bing places"}, "results")
    t.tool("mcp__remembra__session_brief", {}, "brief")  # the relay's own bookkeeping: not work
    t.tool("ToolSearch", {"query": "x"}, "tools")
    t.tool("TodoWrite", {"todos": []}, "ok")
    t.prompt("<system-reminder>\nstay on task\n</system-reminder>\nand now the second listing")
    t.prompt("<local-command-stdout>ok</local-command-stdout>")  # not something the user asked
    t.prompt("Caveat: generated by a command", isMeta=True)
    t.tool("mcp__remembra__send_to_inbox", {"to": "codex"}, "sent")
    t.say(SECRET_REPLY)
    facts = factlib.parse_claude_transcript(t.write(tmp_path / "t.jsonl"))
    assert facts.prompts == 2
    assert facts.tool_calls == {"navigate": 4, "WebSearch": 1, "send_to_inbox": 1}
    assert facts.agent_handoff is False and facts.automated is False
    assert facts.ended_at is not None and facts.started_at is not None
    assert (facts.ended_at - facts.started_at).total_seconds() >= 600


def test_the_parser_sees_an_automated_run_and_the_agents_own_handoff(tmp_path):
    scheduled = Desktop("s-auto", tmp_path, prompt='<scheduled-task name="project-health-check">\nrun the check')
    scheduled.browse(5)
    assert factlib.parse_claude_transcript(scheduled.write(tmp_path / "a.jsonl")).automated is True
    closed = Desktop("s-closed", tmp_path)
    closed.browse(5)
    closed.tool("mcp__remembra__close_session", {"summary": "done"}, '{"status": "ok", "handoff_id": "84cde101"}')
    assert factlib.parse_claude_transcript(closed.write(tmp_path / "c.jsonl")).agent_handoff is True
    refused = Desktop("s-refused", tmp_path)
    refused.browse(5)
    refused.tool("mcp__remembra__close_session", {"summary": "done"}, "permission denied", is_error=True)
    assert factlib.parse_claude_transcript(refused.write(tmp_path / "r.jsonl")).agent_handoff is False


# ---------------------------------------------------------------------------
# the hook close, outside git
# ---------------------------------------------------------------------------


def test_a_hook_close_outside_git_with_tool_work_is_sent(env, monkeypatch, capsys):
    t = Desktop("s-browser", env["folder"])
    t.browse(47)
    for _ in range(8):
        t.tool("WebSearch", {"query": "q"}, "results")
    t.prompt("now check the second directory")
    t.tool("mcp__remembra__send_to_inbox", {"to": "codex"}, "sent")
    t.say(SECRET_REPLY)
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "t.jsonl"), "s-browser", env["folder"])
    assert "nothing to hand off" not in log and "close: sent the handoff of claude-code session s-browser" in log
    (body,) = env["http"].closes()
    notes = body["facts"]["notes"]
    assert "2 prompts" in notes and "56 tool calls" in notes
    assert "navigate 47" in notes and "WebSearch 8" in notes and "send_to_inbox 1" in notes
    assert "not written by the agent" in notes
    assert body["facts"]["facts_source"] == "relay-cli:transcript"
    # nothing that was said leaves the machine
    sent = json.dumps(body)
    assert SECRET_PROMPT not in sent and SECRET_REPLY not in sent and "example.test" not in sent
    # the brief shows the note
    rendered = render_handoff(
        agent_id="claude-code",
        session_id="s-browser",
        project_id="notes",
        closed_at=datetime.now(UTC),
        facts=body["facts"],
        sections=build_sections(body["facts"]),
        summary=None,
        grounding={},
        end_reason="other",
    )
    assert "56 tool calls" in rendered
    assert "Facts: reported as collected by remembra-relay from the session transcript (not checked)." in rendered


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda t: None, id="prompt-only"),
        pytest.param(lambda t: t.browse(2), id="two-tool-calls"),
        pytest.param(lambda t: [t.tool("mcp__remembra__get_inbox", {}, "[]") for _ in range(6)], id="only-relay-bookkeeping"),
    ],
)
def test_a_session_that_did_next_to_nothing_is_still_skipped(env, monkeypatch, capsys, build):
    t = Desktop("s-thin", env["folder"])
    build(t)
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "t.jsonl"), "s-thin", env["folder"])
    assert "close: nothing to hand off for claude-code session s-thin" in log
    assert env["http"].closes() == []


def test_a_scheduled_run_is_still_skipped(env, monkeypatch, capsys):
    t = Desktop("s-sched", env["folder"], prompt='<scheduled-task name="project-health-check">\nrun it')
    t.browse(20)
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "t.jsonl"), "s-sched", env["folder"])
    assert "close: nothing to hand off for claude-code session s-sched" in log and env["http"].closes() == []


def test_the_agents_own_handoff_is_not_followed_by_a_thinner_one(env, monkeypatch, capsys):
    """The agent called close_session (stored under its MCP identity): the hook adds no second handoff on top."""
    t = Desktop("s-own", env["folder"])
    t.browse(30)
    t.tool("mcp__remembra__close_session", {"summary": "listings cleaned"}, '{"status": "ok", "handoff_id": "84cde101"}')
    t.browse(2)
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "t.jsonl"), "s-own", env["folder"])
    assert "close: nothing to hand off for claude-code session s-own" in log and env["http"].closes() == []


# ---------------------------------------------------------------------------
# a clean git checkout (the same rule, through the relay CLI and through crewd)
# ---------------------------------------------------------------------------


def test_read_only_work_in_a_clean_checkout_is_handed_off_and_real_facts_need_no_note(env, monkeypatch, capsys):
    _, clones = make_remote_and_clones(env["tmp"])
    repo = clones["laptop"]
    t = Transcript("s-verify", repo)
    for n in range(38):
        t.bash(f"git log -n {n + 1} --oneline", 0, "abc1234 x")
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "v.jsonl"), "s-verify", repo)
    assert "nothing to hand off" not in log
    (body,) = env["http"].closes()
    assert "1 prompt, 38 tool calls" in body["facts"]["notes"] and "Bash 38" in body["facts"]["notes"]
    assert body["facts"]["facts_source"] == "relay-cli:git+transcript"
    # a session with a commit hands off the commit: no activity note is added to it
    start = git(repo, "rev-parse", "HEAD")
    mine = commit(repo, "m.py", "m\n", "feat: mine")
    parsed = factlib.parse_claude_transcript(env["tmp"] / "v.jsonl")
    merged = factlib.merge_facts(factlib.git_facts(repo, factlib.Deadline(10), start_head=start), parsed, str(repo))
    assert [c["sha"] for c in merged["commits"]] == [mine] and "notes" not in merged


async def test_crewd_hands_off_a_crew_session_that_only_ran_checks(tmp_path):
    """crewd's close goes through the same parser and merge: 52 tool calls in a clean worktree are not "nothing"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "README.md", "# x\n", "init")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
    sess = {
        "toplevel": str(repo),
        "adapter": "claude-code",
        "agent_id": "claude-code",
        "client_session_id": "s-crew-verify",
        "project_id": "demo",
        "started_head": head.strip(),
        "joined_at": 0,
    }
    sent: list[dict[str, Any]] = []

    class Stub:
        def api_for(self, _sess: Any) -> str:
            return "api"

        async def request(self, _api: Any, method: str, path: str, **kw: Any) -> None:
            sent.append({"method": method, "path": path, **kw})

    t = Desktop("s-crew-verify", repo)
    t.browse(14)
    for n in range(38):
        t.bash(f"curl -s https://example.test/health?n={n}", 0, "ok")
    await crewd_mod.Crewd.relay_close(Stub(), sess, "exit", str(t.write(tmp_path / "t.jsonl")))  # type: ignore[arg-type]
    assert [(s["method"], s["path"]) for s in sent] == [("POST", "/session/close")]
    assert "52 tool calls" in sent[0]["json_body"]["facts"]["notes"]


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _rollout(path: Path, cwd: Path, calls: int, session: str = "s-codex") -> Path:
    lines: list[dict[str, Any]] = [
        {"timestamp": "2026-11-07T22:00:00.000Z", "type": "session_meta", "payload": {"id": session, "cwd": str(cwd)}},
        {"timestamp": "2026-11-07T22:00:01.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {
            "timestamp": "2026-11-07T22:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": SECRET_PROMPT}]},
            },
        },
    ]
    for n in range(calls):
        lines.append(
            {
                "timestamp": f"2026-11-07T22:{n + 1:02d}:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "mcp__shopify__get-product",
                    "call_id": f"call_{n}",
                    "arguments": json.dumps({"id": n}),
                },
            }
        )
        lines.append(
            {
                "timestamp": f"2026-11-07T22:{n + 1:02d}:01.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": f"call_{n}", "output": "{}"},
            }
        )
    lines.append(
        {
            "timestamp": "2026-11-07T22:30:00.000Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "update_plan", "call_id": "plan_1", "arguments": '{"plan": []}'},
        }
    )
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_a_codex_session_outside_git_with_tool_work_is_sent(env, monkeypatch, capsys):
    rollout = _rollout(env["tmp"] / "rollout-x.jsonl", env["folder"], calls=13)
    parsed = factlib.parse_codex_rollout(rollout)
    assert parsed.prompts == 1 and parsed.tool_calls == {"get-product": 13}
    log = _hook_close(env, monkeypatch, capsys, rollout, "s-codex", env["folder"], hook="codex")
    assert "nothing to hand off" not in log
    (body,) = env["http"].closes()
    assert "1 prompt, 13 tool calls" in body["facts"]["notes"] and "get-product 13" in body["facts"]["notes"]
    assert SECRET_PROMPT not in json.dumps(body)


def test_an_idle_codex_thread_is_still_skipped(env, monkeypatch, capsys):
    rollout = _rollout(env["tmp"] / "rollout-y.jsonl", env["folder"], calls=0, session="s-idle")
    log = _hook_close(env, monkeypatch, capsys, rollout, "s-idle", env["folder"], hook="codex")
    assert "close: nothing to hand off for codex session s-idle" in log and env["http"].closes() == []


# ---------------------------------------------------------------------------
# the note
# ---------------------------------------------------------------------------


def test_the_note_is_bounded_and_carries_only_tool_names(tmp_path):
    t = Desktop("s-names", tmp_path)
    for n in range(12):
        for _ in range(3):
            t.tool(f"mcp__srv__tool_{n}", {}, "ok")
    t.tool("mcp__srv__we<ird> name\nwith\x1b[31m stuff", {}, "ok")
    parsed = factlib.parse_claude_transcript(t.write(tmp_path / "t.jsonl"))
    note = factlib.activity_note(parsed)
    assert note is not None and len(note) <= 600
    assert "(+7 more)" in note and "<" not in note and "\x1b" not in note and "\n" not in note
    assert factlib.activity_note(None) is None and factlib.activity_note(factlib.TranscriptFacts()) is None
    # typed notes win: the relay's line never replaces what the agent or the user wrote
    merged = factlib.merge_facts({"notes": "halfway through the migration"}, parsed, None)
    assert merged["notes"] == "halfway through the migration"


# ---------------------------------------------------------------------------
# the server (the production routes over SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path):
    from tests.agent_api_harness import build_api

    yield from build_api(tmp_path)


def test_the_server_stores_the_note_and_shows_it_as_the_last_session(api, tmp_path):
    t = Desktop("s-server", tmp_path)
    t.browse(9)
    facts = factlib.merge_facts({}, factlib.parse_claude_transcript(t.write(tmp_path / "t.jsonl")), None)
    facts["facts_source"] = "relay-cli:transcript"
    http = api["http"]
    closed = http.post(
        "/api/v1/session/close",
        json={"agent_id": "claude-code", "session_id": "s-server", "project_id": "notes", "facts": facts, "end_reason": "other"},
    )
    assert closed.status_code == 200, closed.text
    brief = http.get("/api/v1/session/brief", params={"agent_id": "codex", "project_id": "notes"}).json()
    assert brief["handoff"] is not None, brief["rendered"]
    assert "1 prompt, 9 tool calls" in brief["rendered"] and "navigate 9" in brief["rendered"]
    assert "from the session transcript" in brief["rendered"]
