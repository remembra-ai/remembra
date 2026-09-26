"""remembra-relay end to end: the real CLI (subprocess) -> HTTP -> real API -> SQLite.

A uvicorn server runs the production routes on a local port (vector store and
embedder are in-process fakes). Each test builds real git repos: a bare
"origin", clones in different directories (laptop / external drive), commits,
uncommitted edits and a Claude Code JSONL transcript with a failing pytest.
The CLI runs with an isolated HOME so the user's real config is never read.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from remembra.api.router import api_router
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.inbox.manager import InboxManager
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones

SRC = str(Path(__file__).resolve().parents[1] / "src")
SECRET = "sk-proj-" + "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lz"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("relay-server") / "relay.db"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(str(db_path))
        await db.connect()
        await db.init_schema()
        inbox = InboxManager(db)
        await inbox.init_schema()
        settings = Settings(openai_api_key="test", enable_entity_resolution=False)
        service = MemoryService(settings=settings, qdrant=FakeQdrant(), db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
        service.extractor = OneFactExtractor()  # type: ignore[assignment]
        app.state.db = db
        app.state.memory_service = service
        app.state.inbox_manager = inbox
        app.state.audit_logger = AuditLogger(db)
        app.state.sanitizer = ContentSanitizer()
        app.state.pii_detector = None
        yield
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.include_router(api_router)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        assert time.time() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def relay(home: Path, url: str, *args: str, stdin: str | None = None, cwd: Path | None = None, env: dict | None = None):
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": SRC,
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": "rem_test_key_for_e2e",
        **GIT_ENV,
    }
    base.update(env or {})
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(cwd or home),
        env=base,
        timeout=60,
    )
    proc.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    return proc


def _api(url: str, method: str, path: str, **kw: Any) -> Any:
    res = httpx.request(method, f"{url}/api/v1{path}", timeout=10, **kw)
    res.raise_for_status()
    return res.json()


def _two_clones(tmp_path: Path, owner: str):
    """Same repo in two places, with remotes spelled differently (https vs ssh)."""
    _, clones = make_remote_and_clones(tmp_path, ("laptop", "drive"))
    git(clones["laptop"], "remote", "set-url", "origin", f"https://github.com/{owner}/Widget.git")
    git(clones["drive"], "remote", "set-url", "origin", f"git@github.com:{owner.lower()}/widget")
    return clones["laptop"], clones["drive"]


def test_agent_a_closes_agent_b_picks_up_on_another_clone(server, home, tmp_path):
    laptop, drive = _two_clones(tmp_path, "AcmeHandoff")

    # Session start (Claude Code SessionStart hook): records the start head.
    hook_start = json.dumps({"session_id": "sess-A", "cwd": str(laptop), "hook_event_name": "SessionStart"})
    start = relay(home, server, "brief", "--hook", "claude-code", "--agent", "claude-code", stdin=hook_start)
    assert start.returncode == 0, start.stderr
    assert "Last session: none recorded for this project." in start.stdout

    # Agent A works: two commits (not pushed), one uncommitted file.
    commit(laptop, "src/widget.py", "def api(): ...\n", "feat: widget api")
    head = commit(laptop, "tests/test_widget.py", "def test_api(): assert False\n", "test: widget api")
    (laptop / "NOTES.md").write_text("wip\n")

    t = Transcript("sess-A", laptop)
    t.bash("pytest -q tests/test_widget.py", 1, "F\n=== 1 failed, 2 passed in 0.10s ===")
    t.bash("npm run build", 0, "ok")
    t.bash(f"export OPENAI_API_KEY={SECRET} && make deploy", 2, "deploy failed")
    t.tool("Edit", {"file_path": str(laptop / "src" / "widget.py"), "old_string": "a", "new_string": "b"}, "ok")
    t.tool(
        "TodoWrite",
        {"todos": [{"content": "make test_api pass", "status": "pending", "activeForm": "x"}]},
        "ok",
    )
    transcript = t.write(tmp_path / "sess-A.jsonl")

    hook_end = json.dumps(
        {
            "session_id": "sess-A",
            "transcript_path": str(transcript),
            "cwd": str(laptop),
            "hook_event_name": "SessionEnd",
            "reason": "logout",
        }
    )
    close = relay(home, server, "close", "--hook", "claude-code", "--agent", "claude-code", stdin=hook_end)
    assert close.returncode == 0 and close.stdout == "", close.stderr
    assert close.stderr == ""

    # Agent B: a different agent id, a different clone path of the same repo.
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(drive))
    assert brief.returncode == 0, brief.stderr
    out = brief.stdout
    lines = out.splitlines()
    assert lines[0] == "# Remembra brief · project widget · you are codex"
    assert lines[1] == '<remembra-data untrusted="true">'
    last = lines[3]
    assert last.startswith(f"Last session: claude-code (self-declared), just now, on main@{head[:7]}: done: ")
    assert "feat: widget api" in last and "test: widget api" in last
    assert "NOT done: TODO: make test_api pass" in last
    assert "uncommitted changes in 1 file(s): NOTES.md" in last
    assert "failing: FAILING: pytest -q tests/test_widget.py (1 failed, 2 passed in 0.10s)" in last
    assert "next (derived from the recorded facts): fix the failing run: pytest -q tests/test_widget.py" in last
    assert SECRET not in out

    # The stored handoff: one memory, correct sections, no raw transcript, no secret.
    handoffs = _api(server, "GET", "/timeline", params={"project_id": "widget", "memory_type": "handoff"})
    assert handoffs["total"] == 1
    content = handoffs["memories"][0]["content"]
    assert "2 commit(s) not pushed to origin/main" in content
    assert "ended: logout" in content
    assert SECRET not in json.dumps(handoffs)
    assert "[REDACTED:" in json.dumps(handoffs["memories"][0]["metadata"]) or "make deploy" in content

    # Re-closing the same session updates the handoff instead of adding one.
    (laptop / "NOTES.md").unlink()
    again = relay(home, server, "close", "--hook", "claude-code", "--agent", "claude-code", stdin=hook_end)
    assert again.returncode == 0, again.stderr
    handoffs = _api(server, "GET", "/timeline", params={"project_id": "widget", "memory_type": "handoff"})
    assert handoffs["total"] == 1
    assert "uncommitted" not in handoffs["memories"][0]["content"]

    trail = relay(home, server, "trail", "--cwd", str(drive))
    assert trail.returncode == 0
    assert "Trail · project widget · 1 entries" in trail.stdout
    assert "claude-code" in trail.stdout and f"main@{head[:7]}" in trail.stdout


def test_close_dry_run_shows_payload_without_raw_transcript(server, home, tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", "https://github.com/acme/dryrun.git")
    t = Transcript("sess-D", repo)
    t.bash("pytest -q", 1, "=== 2 failed in 0.3s ===")
    t.lines[0]["message"]["content"] = "PRIVATE USER PROMPT THAT MUST NOT LEAVE THE MACHINE"
    transcript = t.write(tmp_path / "d.jsonl")
    proc = relay(
        home, server, "close", "--agent", "claude-code", "--cwd", str(repo), "--transcript", str(transcript), "--dry-run"
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["session_id"] == "sess-D"  # taken from the transcript
    assert payload["project"]["git_remote"] == "https://github.com/acme/dryrun.git"
    assert payload["facts"]["tests"] == [{"cmd": "pytest -q", "passed": False, "summary": "2 failed in 0.3s"}]
    assert "PRIVATE USER PROMPT" not in proc.stdout
    assert payload["facts"]["commits"] == []  # the transcript shows no commit by this agent


def test_close_never_blocks_when_server_is_down(home, tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    dead = f"http://127.0.0.1:{_free_port()}"
    proc = relay(home, dead, "close", "--agent", "claude-code", "--cwd", str(clones["laptop"]))
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "close failed" in proc.stderr
    assert proc.elapsed < 10  # type: ignore[attr-defined]

    brief = relay(home, dead, "brief", "--hook", "claude-code", stdin="{}")
    assert brief.returncode == 0 and "Remembra brief unavailable" in brief.stdout

    no_key = relay(home, dead, "close", "--agent", "x", env={"REMEMBRA_API_KEY": ""})
    assert no_key.returncode == 0 and "no API key" in no_key.stderr

    bad_args = relay(home, dead, "close", "--no-such-flag")
    assert bad_args.returncode == 0


def test_hook_payload_on_hanging_stdin_does_not_block(server, home, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.relay.cli", "brief", "--hook", "claude-code", "--cwd", str(tmp_path)],
        stdin=subprocess.PIPE,  # kept open, never written: must not hang
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTHONPATH": SRC,
            "REMEMBRA_URL": server,
            "REMEMBRA_API_KEY": "rem_x",
        },
    )
    try:
        out, _ = proc.communicate(timeout=15)
    finally:
        proc.kill()
    assert proc.returncode == 0
    assert "Last session" in out


def test_non_git_directory_uses_configured_project(server, home, tmp_path):
    workdir = tmp_path / "plain"
    workdir.mkdir()
    env = {"REMEMBRA_PROJECT": "clawdbot"}
    close = relay(home, server, "close", "--agent", "gemini", "--cwd", str(workdir), "--notes", "non-git notes", env=env)
    assert close.returncode == 0, close.stderr
    assert "project clawdbot" in close.stdout
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(workdir), "--format", "json", env=env)
    data = json.loads(brief.stdout)
    assert data["project_id"] == "clawdbot"
    assert data["handoff"]["agent_id"] == "gemini"


def test_config_discovered_from_claude_json_without_env(server, home, tmp_path):
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remembra": {
                        "env": {"REMEMBRA_URL": server, "REMEMBRA_API_KEY": "rem_from_file", "REMEMBRA_AGENT_ID": "claude-code"}
                    }
                }
            }
        )
    )
    _, clones = make_remote_and_clones(tmp_path)
    git(clones["laptop"], "remote", "set-url", "origin", "https://github.com/acme/cfg.git")
    proc = relay(
        home, "http://127.0.0.1:9", "close", "--cwd", str(clones["laptop"]), env={"REMEMBRA_URL": "", "REMEMBRA_API_KEY": ""}
    )
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr
    assert "project cfg" in proc.stdout
    trail = _api(server, "GET", "/trail", params={"project_id": "cfg"})
    assert trail["items"][0]["agent_id"] == "claude-code"


def test_gemini_style_hook_gets_json_only_stdout(server, home, tmp_path):
    payload = json.dumps({"session_id": "g1", "cwd": str(tmp_path)})
    proc = relay(home, server, "brief", "--hook", "gemini", stdin=payload)
    data = json.loads(proc.stdout)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Last session" in data["hookSpecificOutput"]["additionalContext"]
    cursor = relay(
        home, server, "brief", "--hook", "cursor", stdin=json.dumps({"conversation_id": "c1", "workspace_roots": [str(tmp_path)]})
    )
    assert "additional_context" in json.loads(cursor.stdout)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

LEGACY = "python3 /Users/x/remembra/integrations/claude-code/session_start.py"


def _claude_settings(home: Path) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": LEGACY, "timeout": 15}]}],
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}],
                },
            },
            indent=2,
        )
    )
    return path


def test_connect_dry_run_then_apply_claude_code(home):
    settings = _claude_settings(home)
    before = settings.read_text()
    relay_cmd = "/opt/bin/remembra-relay"
    dry = relay(home, "http://x", "connect", "--agent", "claude-code", "--relay-command", relay_cmd)
    assert dry.returncode == 0, dry.stderr
    assert "(dry run: re-run with --apply" in dry.stdout
    assert f"SessionStart: add `{relay_cmd} brief --hook claude-code --agent claude-code`" in dry.stdout
    assert "remove legacy hook" in dry.stdout
    assert "rem_test_key_for_e2e" not in dry.stdout
    assert settings.read_text() == before

    applied = relay(home, "http://x", "connect", "--agent", "claude-code", "--apply", "--relay-command", relay_cmd)
    assert applied.returncode == 0, applied.stderr
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}]
    assert data["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": f"{relay_cmd} brief --hook claude-code --agent claude-code", "timeout": 15}]}
    ]
    close_hook = {"type": "command", "command": f"{relay_cmd} close --hook claude-code --agent claude-code", "timeout": 15}
    assert data["hooks"]["SessionEnd"] == [{"hooks": [close_hook]}]
    # A usage/billing limit ends the turn, not the session: StopFailure writes the handoff then (and PreCompact).
    assert data["hooks"]["StopFailure"] == [
        {"matcher": "rate_limit|billing_error|account_on_hold|cloud_credential_error", "hooks": [close_hook]}
    ]
    assert data["hooks"]["PreCompact"] == [{"hooks": [close_hook]}]
    backups = list(settings.parent.glob("settings.json.bak-relay-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    assert "REMEMBRA_API_KEY" not in settings.read_text()

    again = relay(home, "http://x", "connect", "--agent", "claude-code", "--apply", "--relay-command", relay_cmd)
    assert "already connected, no change" in again.stdout
    assert len(list(settings.parent.glob("settings.json.bak-relay-*"))) == 1


def test_connect_unverified_adapters_need_explicit_opt_in(home):
    relay_cmd = "/opt/bin/remembra-relay"
    skipped = relay(home, "http://x", "connect", "--agent", "gemini", "--apply", "--relay-command", relay_cmd)
    assert "UNVERIFIED" in skipped.stdout and "skipped: unverified adapter" in skipped.stdout
    assert not (home / ".gemini" / "settings.json").exists()

    written = relay(
        home,
        "http://x",
        "connect",
        "--agent",
        "gemini",
        "--agent",
        "kimi",
        "--apply",
        "--include-unverified",
        "--relay-command",
        relay_cmd,
    )
    assert written.returncode == 0, written.stderr
    gem = json.loads((home / ".gemini" / "settings.json").read_text())
    assert gem["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == f"{relay_cmd} close --hook gemini --agent gemini"
    kimi = (home / ".kimi" / "config.toml").read_text()
    assert kimi.count("[[hooks]]") == 2
    rerun = relay(home, "http://x", "connect", "--agent", "kimi", "--apply", "--include-unverified", "--relay-command", relay_cmd)
    assert "already connected" in rerun.stdout
    assert (home / ".kimi" / "config.toml").read_text() == kimi


def test_connect_without_a_key_warns_and_fails_but_still_writes_hooks(home):
    settings = _claude_settings(home)
    relay_cmd = "/opt/bin/remembra-relay"
    no_key = {"REMEMBRA_API_KEY": "", "REMEMBRA_URL": ""}
    dry = relay(home, "", "connect", "--agent", "claude-code", "--relay-command", relay_cmd, env=no_key)
    assert dry.returncode == 1
    assert '"api_key": "missing"' in dry.stdout
    assert dry.stderr.count("no Remembra API key found") == 2  # at the top and again at the end
    assert "remembra-install --all --url <your server URL>" in dry.stderr
    assert "--api-key" not in dry.stderr  # the fix never asks for the key on the command line
    assert "\033[" not in dry.stderr  # not a terminal: no color codes

    applied = relay(home, "", "connect", "--agent", "claude-code", "--apply", "--relay-command", relay_cmd, env=no_key)
    assert applied.returncode == 1 and "no Remembra API key found" in applied.stderr
    assert "written" in applied.stdout  # the hooks read the key at run time, so they are still installed
    assert json.loads(settings.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["command"].startswith(relay_cmd)

    # With the credentials file remembra-install writes, the same command is clean.
    creds = home / ".remembra" / "credentials"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"api_key": "rem_from_credentials", "url": "http://x"}))
    ok = relay(home, "", "connect", "--agent", "claude-code", "--relay-command", relay_cmd, env=no_key)
    assert ok.returncode == 0 and ok.stderr == "", ok.stderr
    assert '"api_key": "set"' in ok.stdout and "rem_from_credentials" not in ok.stdout


def test_connect_apply_lists_the_unverified_adapters_it_skipped(home):
    relay_cmd = "/opt/bin/remembra-relay"
    out = relay(home, "http://x", "connect", "--agent", "qwen", "--agent", "gemini", "--apply", "--relay-command", relay_cmd)
    assert out.returncode == 0, out.stderr
    assert "Not written (unverified adapters): gemini, qwen." in out.stdout
    assert "remembra-relay connect --apply --include-unverified --agent gemini --agent qwen" in out.stdout
    dry = relay(home, "http://x", "connect", "--agent", "qwen", "--relay-command", relay_cmd)
    assert "Not written (unverified adapters)" not in dry.stdout  # a dry run writes nothing anyway


def test_connect_codex_writes_three_hooks_and_says_they_need_trust(home):
    """Codex skips a hook until the user trusts it in /hooks, silently. connect must say so."""
    relay_cmd = "/opt/bin/remembra-relay"
    trust = "Open Codex and run /hooks to trust the three remembra-relay hooks"
    dry = relay(home, "http://x", "connect", "--agent", "codex", "--relay-command", relay_cmd)
    assert dry.returncode == 0 and trust in dry.stdout and "(dry run" in dry.stdout
    out = relay(home, "http://x", "connect", "--agent", "codex", "--apply", "--relay-command", relay_cmd)
    assert out.returncode == 0, out.stderr
    assert "OpenAI Codex CLI (verified)" in out.stdout and "Not written" not in out.stdout
    assert trust in out.stdout and "they will not run until you do" in out.stdout
    # the note says which build was run and how, not just "verified"
    assert (
        "note: Verified with codex-cli 0.155.0-alpha.16.4 (prerelease), run through codex exec "
        "with a local stand-in for the model; other versions have not been run." in out.stdout
    )
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    assert {e: [h["hooks"][0] for h in hooks[e]] for e in hooks} == {
        "SessionStart": [{"type": "command", "command": f"{relay_cmd} brief --hook codex --agent codex", "timeout": 15}],
        "UserPromptSubmit": [
            {"type": "command", "command": f"{relay_cmd} brief --hook codex --agent codex --once", "timeout": 15}
        ],
        # Codex allows SessionEnd at most 3 s; close detaches to fit.
        "SessionEnd": [{"type": "command", "command": f"{relay_cmd} close --hook codex --agent codex", "timeout": 3}],
    }
    again = relay(home, "http://x", "connect", "--agent", "codex", "--apply", "--relay-command", relay_cmd)
    assert "already connected, no change" in again.stdout and trust in again.stdout


def test_connect_agents_md_block_is_idempotent(home, tmp_path):
    md = tmp_path / "AGENTS.md"
    md.write_text("# Project rules\n\nBe nice.\n")
    relay_cmd = "/opt/bin/remembra-relay"
    relay(home, "http://x", "connect", "--agent", "cursor", "--agents-md", str(md), "--apply", "--relay-command", relay_cmd)
    text = md.read_text()
    assert text.startswith("# Project rules\n\nBe nice.\n\n<!-- remembra-relay:start -->")
    assert f"`{relay_cmd} close --agent <your-agent-id>`" in text
    relay(home, "http://x", "connect", "--agent", "cursor", "--agents-md", str(md), "--apply", "--relay-command", relay_cmd)
    assert md.read_text() == text
