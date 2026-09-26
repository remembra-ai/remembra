"""remembra-relay: handoffs at a usage limit (StopFailure / PreCompact), the local outbox,
``status`` and ``disconnect``.

End to end: the real CLI in a subprocess, HTTP to the production routes on a
local uvicorn (vector store and embedder faked), real git repos and a
Claude Code transcript, the recorded Claude Code hook payloads in
``tests/fixtures/claude_code`` and an isolated HOME for every test.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
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
from remembra.relay import outbox
from remembra.relay.adapters import REGISTRY
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones

SRC = str(Path(__file__).resolve().parents[1] / "src")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_code"
KEY = "rem_test_key_for_outbox_0123456789"
RELAY_CMD = "/opt/bin/remembra-relay"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def api_server(db_path: Path, port: int | None = None) -> Iterator[str]:
    """The production API routes on a local port (auth disabled, fakes for vectors and embeddings)."""

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
    port = port or _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        assert time.time() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(10)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with api_server(tmp_path_factory.mktemp("relay-stop") / "relay.db") as url:
        yield url


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def relay(home: Path, url: str, *args: str, stdin: str | None = None, env: dict | None = None):
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": SRC,
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": KEY,
        **GIT_ENV,
    }
    base.update(env or {})
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(home),
        env=base,
        timeout=60,
    )
    proc.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    return proc


def _api(url: str, method: str, path: str, **kw: Any) -> Any:
    res = httpx.request(method, f"{url}/api/v1{path}", timeout=10, **kw)
    res.raise_for_status()
    return res.json()


def _fixture(name: str, **overrides: Any) -> str:
    """A recorded hook payload with this test's cwd / session / transcript swapped in."""
    payload = json.loads((FIXTURES / name).read_text())["payload"]
    payload.update(overrides)
    return json.dumps(payload)


def _repo(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    _, clones = make_remote_and_clones(tmp_path, ("laptop", "drive"))
    for clone in clones.values():
        git(clone, "remote", "set-url", "origin", f"https://github.com/acme/{slug}.git")
    return clones["laptop"], clones["drive"]


def _work(repo: Path, tmp_path: Path, session: str) -> Path:
    """One commit, three uncommitted files and a transcript with a failing test run."""
    commit(repo, "src/app.py", "def app(): ...\n", "feat: app skeleton")
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("wip\n")
    t = Transcript(session, repo)
    t.bash("pytest -q", 1, "=== 1 failed, 4 passed in 0.2s ===")
    return t.write(tmp_path / f"{session}.jsonl")


def _handoffs(url: str, project: str) -> list[dict[str, Any]]:
    return _api(url, "GET", "/timeline", params={"project_id": project, "memory_type": "handoff"})["memories"]


def _trail(url: str, project: str) -> list[dict[str, Any]]:
    return _api(url, "GET", "/trail", params={"project_id": project})["items"]


# ---------------------------------------------------------------------------
# R-5: the handoff is written when Claude Code stops on a limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["error", "error_type"])
def test_stopfailure_rate_limit_writes_the_handoff_while_the_session_stays_open(server, home, tmp_path, field):
    slug = f"limit-{field.replace('_', '')}"
    laptop, drive = _repo(tmp_path, slug)
    session = f"sess-{field}"
    start = relay(
        home,
        server,
        "brief",
        "--hook",
        "claude-code",
        "--agent",
        "claude-code",
        stdin=json.dumps({"session_id": session, "cwd": str(laptop)}),
    )
    assert start.returncode == 0, start.stderr
    transcript = _work(laptop, tmp_path, session)

    recorded = json.loads((FIXTURES / "stopfailure-rate_limit.json").read_text())["payload"]
    payload = dict(recorded, session_id=session, cwd=str(laptop), transcript_path=str(transcript))
    if field == "error_type":  # the name the hooks reference uses
        payload["error_type"] = payload.pop("error")
    stop = relay(home, server, "close", "--hook", "claude-code", "--agent", "claude-code", stdin=json.dumps(payload))
    assert stop.returncode == 0 and stop.stdout == "" and stop.stderr == "", stop.stderr

    trail = _trail(server, slug)
    assert len(trail) == 1
    detail = trail[0]["detail"]
    assert detail["end_reason"] == "rate_limit"
    assert trail[0]["headline"].startswith("stopped: rate_limit · 1 commit(s), last: feat: app skeleton")
    [handoff] = _handoffs(server, slug)
    relay_meta = handoff["metadata"]["relay"]
    assert relay_meta["session_id"] == session
    assert relay_meta["facts_source"] == "relay-cli:git+transcript"  # built from git AND the transcript
    assert relay_meta["uncommitted_count"] == 3
    assert any("pytest -q" in f for f in relay_meta["failing"])
    assert "ended: rate_limit" in handoff["content"]

    # Another agent, another clone, while the Claude Code session is still open.
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(drive))
    last = next(line for line in brief.stdout.splitlines() if line.startswith("Last session:"))
    assert last.startswith("Last session: claude-code (self-declared), just now, stopped: rate_limit, on main@")
    assert "uncommitted changes in 3 file(s)" in last and "FAILING: pytest -q" in last


def test_a_later_session_end_supersedes_the_stop_handoff_in_either_order(server, home, tmp_path):
    laptop, _ = _repo(tmp_path, "limit-order")
    transcript = _work(laptop, tmp_path, "sess-order")
    common = {"session_id": "sess-order", "cwd": str(laptop), "transcript_path": str(transcript)}
    stop = _fixture("stopfailure-rate_limit.json", **common)
    end = _fixture("sessionend-after-rate_limit.json", **common)
    args = ("close", "--hook", "claude-code", "--agent", "claude-code")

    assert relay(home, server, *args, stdin=stop).returncode == 0
    assert relay(home, server, *args, stdin=end).returncode == 0
    trail = _trail(server, "limit-order")
    assert len(trail) == 1 and trail[0]["detail"]["end_reason"] == "other"
    assert len(_handoffs(server, "limit-order")) == 1  # one current row; the stop handoff is superseded

    # StopFailure is fire-and-forget: it can land after SessionEnd. Still one current handoff.
    assert relay(home, server, *args, stdin=stop).returncode == 0
    trail = _trail(server, "limit-order")
    assert len(trail) == 1 and trail[0]["detail"]["end_reason"] == "rate_limit"


def test_billing_error_and_pre_compact_are_named_in_the_brief(server, home, tmp_path):
    laptop, drive = _repo(tmp_path, "limit-billing")
    transcript = _work(laptop, tmp_path, "sess-bill")
    args = ("close", "--hook", "claude-code", "--agent", "claude-code")
    billing = _fixture("stopfailure-billing_error.json", session_id="sess-bill", cwd=str(laptop), transcript_path=str(transcript))
    assert relay(home, server, *args, stdin=billing).returncode == 0
    assert _trail(server, "limit-billing")[0]["detail"]["end_reason"] == "billing_error"

    compact = json.dumps(
        {
            "session_id": "sess-compact",
            "transcript_path": str(transcript),
            "cwd": str(laptop),
            "hook_event_name": "PreCompact",
            "trigger": "auto",
            "custom_instructions": "",
        }
    )
    assert relay(home, server, *args, stdin=compact).returncode == 0
    reasons = {item["session_id"]: item["detail"]["end_reason"] for item in _trail(server, "limit-billing")}
    assert reasons == {"sess-bill": "billing_error", "sess-compact": "pre-compact:auto"}
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(drive))
    assert "still open (saved before context compaction)" in brief.stdout


def test_connect_upgrades_an_old_two_event_install_in_place(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    old_hook = lambda verb: {  # noqa: E731
        "hooks": [{"type": "command", "command": f"{RELAY_CMD} {verb} --hook claude-code --agent claude-code", "timeout": 15}]
    }
    before = {
        "model": "opus",
        "hooks": {
            "SessionStart": [old_hook("brief")],
            "SessionEnd": [old_hook("close")],
            "Stop": [{"hooks": [{"type": "command", "command": "afplay done.aiff"}]}],
        },
    }
    settings.write_text(json.dumps(before, indent=2))
    original = settings.read_text()

    dry = relay(home, "http://x", "connect", "--agent", "claude-code", "--relay-command", RELAY_CMD)
    assert "StopFailure (matcher `rate_limit|billing_error|account_on_hold|cloud_credential_error`): add" in dry.stdout
    assert "PreCompact: add" in dry.stdout
    assert settings.read_text() == original

    applied = relay(home, "http://x", "connect", "--agent", "claude-code", "--apply", "--relay-command", RELAY_CMD)
    assert applied.returncode == 0, applied.stderr
    data = json.loads(settings.read_text())
    assert data["hooks"]["SessionStart"] == before["hooks"]["SessionStart"]
    assert data["hooks"]["SessionEnd"] == before["hooks"]["SessionEnd"]
    assert data["hooks"]["Stop"] == before["hooks"]["Stop"] and data["model"] == "opus"
    assert data["hooks"]["StopFailure"] == [
        {"matcher": "rate_limit|billing_error|account_on_hold|cloud_credential_error", **old_hook("close")}
    ]
    assert data["hooks"]["PreCompact"] == [old_hook("close")]
    backups = list(settings.parent.glob("settings.json.bak-relay-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600

    again = relay(home, "http://x", "connect", "--agent", "claude-code", "--apply", "--relay-command", RELAY_CMD)
    assert "already connected, no change" in again.stdout
    assert len(list(settings.parent.glob("settings.json.bak-relay-*"))) == 1


# ---------------------------------------------------------------------------
# R-6: the outbox
# ---------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_close_while_the_api_is_down_is_queued_then_delivered_by_the_next_brief(tmp_path, home):
    laptop, drive = _repo(tmp_path, "outage")
    transcript = _work(laptop, tmp_path, "sess-outage")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"  # nothing listens yet: the API is down
    secret = "sk-proj-" + "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lz"
    end = json.dumps(
        {
            "session_id": "sess-outage",
            "cwd": str(laptop),
            "transcript_path": str(transcript),
            "hook_event_name": "SessionEnd",
            "reason": "logout",
        }
    )
    close = relay(home, url, "close", "--hook", "claude-code", "--agent", "claude-code", stdin=end)
    assert close.returncode == 0 and close.stdout == ""
    assert "close failed" in close.stderr and "queued" in close.stderr
    assert close.elapsed < 10  # type: ignore[attr-defined]

    [entry] = list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    assert entry.name.startswith("claude-code-sess-outage")
    assert _mode(entry) == 0o600 and _mode(entry.parent) == 0o700
    stored = json.loads(entry.read_text())
    assert stored["session_id"] == "sess-outage" and stored["payload"]["end_reason"] == "logout"
    assert stored["url"] == url and KEY not in entry.read_text()
    log = home / ".remembra" / "relay" / "relay.log"
    assert _mode(log) == 0o600 and "outbox: queued claude-code session sess-outage" in log.read_text()

    # A note typed with a secret in it is stored redacted, as the server would store it.
    noted = relay(home, url, "close", "--agent", "gemini", "--cwd", str(laptop), "--notes", f"key is {secret}")
    assert noted.returncode == 0
    assert secret not in "".join(p.read_text() for p in entry.parent.glob("*.json"))

    status = relay(home, url, "status")
    assert status.returncode == 1  # something needs attention
    assert "queue: 2 handoff(s) waiting" in status.stdout and "not checked (server unreachable" in status.stdout
    as_json = json.loads(relay(home, url, "status", "--format", "json").stdout)
    assert as_json["queue_depth"] == 2
    assert {q["agent_id"] for q in as_json["queue"]} == {"claude-code", "gemini"}
    assert "ConnectError" in as_json["agents"]["claude-code"]["last_failure"]["error"]

    # The API comes back on the same URL; the next brief (another agent, another clone) sends the queue first.
    with api_server(tmp_path / "back.db", port) as back:
        brief = relay(home, back, "brief", "--agent", "codex", "--cwd", str(drive))
        assert brief.returncode == 0, brief.stderr
        assert brief.stdout.splitlines()[0] == "Remembra: sent 2 queued handoffs from claude-code, gemini."
        assert "Last session: " in brief.stdout
        items = _trail(back, "outage")
        assert {i["session_id"] for i in items} >= {"sess-outage"}
        original = next(i for i in items if i["agent_id"] == "claude-code")
        assert original["detail"]["end_reason"] == "logout"
        assert not list(entry.parent.glob("*.json"))
        assert "outbox: delivered claude-code session sess-outage" in log.read_text()

        ok = relay(home, back, "status")
        assert ok.returncode == 0, ok.stdout
        assert "key: accepted by the server" in ok.stdout and "queue: 0 handoff(s)" in ok.stdout


def test_a_replayed_close_creates_no_duplicate_handoff(tmp_path, home):
    laptop, _ = _repo(tmp_path, "replay-twice")
    transcript = _work(laptop, tmp_path, "sess-twice")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    end = json.dumps({"session_id": "sess-twice", "cwd": str(laptop), "transcript_path": str(transcript), "reason": "other"})
    relay(home, url, "close", "--hook", "claude-code", "--agent", "claude-code", stdin=end)
    [entry] = list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    saved = entry.read_text()
    with api_server(tmp_path / "twice.db", port) as back:
        assert relay(home, back, "brief", "--agent", "codex", "--cwd", str(laptop)).returncode == 0
        # A crash after the send but before the delete leaves the entry: it is sent again.
        entry.write_text(saved)
        again = relay(home, back, "brief", "--agent", "codex", "--cwd", str(laptop))
        assert "sent 1 queued handoff from claude-code" in again.stdout
        assert len(_handoffs(back, "replay-twice")) == 1
        assert len(_trail(back, "replay-twice")) == 1


def test_a_delivered_close_drops_the_queued_copy_of_the_same_session(tmp_path, home, server):
    laptop, _ = _repo(tmp_path, "newer-wins")
    port = _free_port()
    first = relay(
        home,
        f"http://127.0.0.1:{port}",
        "close",
        "--agent",
        "claude-code",
        "--session-id",
        "S",
        "--cwd",
        str(laptop),
        "--notes",
        "old",
    )
    assert "queued" in first.stderr
    second = relay(home, server, "close", "--agent", "claude-code", "--session-id", "S", "--cwd", str(laptop), "--notes", "new")
    assert second.returncode == 0 and "Remembra handoff" in second.stdout
    assert not list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    [handoff] = _handoffs(server, "newer-wins")
    assert "Notes (agent): new" in handoff["content"]


def test_a_queued_close_goes_before_the_next_close_and_keeps_its_place(tmp_path, home):
    """R-6: codex's close is queued while the API is down; claude-code closes later, once it is back.
    The brief must name claude-code's session as the last one, not the replayed older one."""
    laptop, drive = _repo(tmp_path, "order")
    port = _free_port()
    old = relay(home, f"http://127.0.0.1:{port}", "close", "--agent", "codex", "--session-id", "A-old", "--cwd", str(laptop))
    assert "queued" in old.stderr
    [entry] = list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    ended = json.loads(entry.read_text())["payload"]["closed_at"]
    time.sleep(1.1)
    with api_server(tmp_path / "order.db", port) as back:
        new = relay(home, back, "close", "--agent", "claude-code", "--session-id", "B-new", "--cwd", str(laptop))
        assert new.returncode == 0 and "Remembra handoff" in new.stdout, new.stderr
        assert not list(entry.parent.glob("*.json"))
        assert "outbox: delivered codex session A-old" in (home / ".remembra" / "relay" / "relay.log").read_text()

        brief = relay(home, back, "brief", "--agent", "gemini", "--cwd", str(drive))
        assert brief.returncode == 0, brief.stderr
        [last] = [line for line in brief.stdout.splitlines() if line.startswith("Last session:")]
        assert last.startswith("Last session: claude-code (self-declared), just now")
        assert "- last_agent:order: claude-code (session B-new)" in brief.stdout
        assert [i["session_id"] for i in _trail(back, "order")] == ["B-new", "A-old"]  # sent in the order they ended
        codex = next(h for h in _handoffs(back, "order") if h["metadata"]["agent_id"] == "codex")
        assert codex["metadata"]["relay"]["closed_at"] == ended  # the time it was closed, not when it arrived


class _FixedStatus(http.server.BaseHTTPRequestHandler):
    status = 401
    body = {"detail": "Invalid or revoked API key"}

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        data = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _reply

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def fixed_status_server(status: int, detail: str) -> Iterator[str]:
    handler = type("Handler", (_FixedStatus,), {"status": status, "body": {"detail": detail}})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def test_a_revoked_key_is_named_at_the_top_of_the_next_brief(tmp_path, home):
    laptop, _ = _repo(tmp_path, "revoked")
    with fixed_status_server(401, "Invalid or revoked API key") as url:
        close = relay(home, url, "close", "--agent", "claude-code", "--session-id", "R", "--cwd", str(laptop))
        assert close.returncode == 0 and "HTTP 401" in close.stderr and "queued" in close.stderr
        brief = relay(
            home,
            url,
            "brief",
            "--hook",
            "claude-code",
            "--agent",
            "claude-code",
            stdin=json.dumps({"session_id": "R2", "cwd": str(laptop)}),
        )
        assert brief.returncode == 0
        first = brief.stdout.splitlines()[0]
        assert first.startswith("Remembra: your API key was rejected (HTTP 401; the key came from env)")
        assert "1 handoff (1 from claude-code) could not be sent yet" in brief.stdout
        as_json = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(laptop), "--format", "json")
        assert any("rejected" in n for n in json.loads(as_json.stdout)["notices"])

        status = relay(home, url, "status")
        assert status.returncode == 1
        assert "key: REJECTED by the server (HTTP 401)" in status.stdout
        assert "queue: 1 handoff(s) waiting" in status.stdout
        offline = json.loads(relay(home, url, "status", "--no-check", "--format", "json").stdout)
        assert offline["key"]["state"] == "rejected" and offline["queue_depth"] == 1


def test_a_body_the_server_rejects_is_not_queued_forever(tmp_path, home):
    laptop, _ = _repo(tmp_path, "unprocessable")
    with fixed_status_server(422, "agent_id: invalid") as url:
        close = relay(home, url, "close", "--agent", "claude-code", "--session-id", "U", "--cwd", str(laptop))
        assert close.returncode == 0 and "HTTP 422" in close.stderr and "queued" not in close.stderr
    assert not list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    assert "not queued, the server rejected the body" in (home / ".remembra" / "relay" / "relay.log").read_text()


class _ScopedKey(http.server.BaseHTTPRequestHandler):
    """A key scoped to claude-code: closes written as codex are refused (403), the rest accepted."""

    posts: list[str] = []

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        agent = str(body.get("agent_id"))
        self.posts.append(f"{agent}:{body.get('session_id')}")
        if agent == "codex":
            self._send(403, {"detail": "This key cannot write as agent codex"})
        else:
            self._send(200, {"ok": True, "memory_id": "m1"})

    def do_GET(self) -> None:
        self._send(200, {"rendered": "Remembra brief"})

    def log_message(self, *args: Any) -> None:
        pass


def test_a_refused_queued_handoff_does_not_hold_back_the_rest(home):
    """A 403 for one entry (a key that may not write as codex) keeps that entry queued but the newer
    entries behind it are still delivered, and it goes last on the next run."""
    handler = type("Handler", (_ScopedKey,), {"posts": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for agent, session in (("codex", "C-old"), ("codex", "C-old2"), ("claude-code", "A-new"), ("gemini", "G-new")):
            assert outbox.enqueue(home, _payload(agent, session), url=url, config_source="env", error="timeout")
            time.sleep(0.02)
        first = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert first.returncode == 0, first.stderr
        # The oldest codex entry is refused; the second codex entry (same key, agent, project) is not tried
        # in the same run; the newer claude-code and gemini entries are delivered.
        assert handler.posts == ["codex:C-old", "claude-code:A-new", "gemini:G-new"]
        assert sorted(e.session_id for e in outbox.pending(home)) == ["C-old", "C-old2"]
        assert "refused this key (HTTP 403)" in first.stdout
        assert "outbox: refused codex session C-old (kept)" in outbox.log_path(home).read_text()
        [refused] = [e for e in outbox.pending(home) if e.session_id == "C-old"]
        assert refused.data["last_status"] == 403 and "cannot write as agent codex" in refused.data["last_error"]

        # Next run: the refused C-old goes after everything else; the newer entry is still delivered and
        # the key is asked once per (agent, project), not once per queued codex entry.
        assert outbox.enqueue(home, _payload("claude-code", "A-later"), url=url, config_source="env", error="timeout")
        handler.posts.clear()
        second = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert second.returncode == 0, second.stderr
        assert handler.posts == ["codex:C-old2", "claude-code:A-later"]
        third = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert third.returncode == 0 and handler.posts[2:] == ["codex:C-old"]  # both refused: oldest first again
        status = relay(home, url, "status", "--no-check")
        assert "queue: 2 handoff(s) waiting" in status.stdout and "HTTP 403" in status.stdout
    finally:
        srv.shutdown()


def test_close_without_a_key_is_queued_and_sent_once_a_key_exists(tmp_path, home, server):
    laptop, _ = _repo(tmp_path, "keyless")
    no_key = {"REMEMBRA_API_KEY": ""}  # REMEMBRA_URL names the server, the key is not set up yet
    close = relay(home, server, "close", "--agent", "claude-code", "--session-id", "K", "--cwd", str(laptop), env=no_key)
    assert "no API key" in close.stderr and "queued" in close.stderr
    [entry] = list((home / ".remembra" / "relay" / "outbox").glob("*.json"))
    stored = json.loads(entry.read_text())
    assert stored["url"] == server and stored["config_source"] == "none"  # kept for the server configured then
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(laptop))
    assert "sent 1 queued handoff from claude-code" in brief.stdout
    assert _trail(server, "keyless")[0]["session_id"] == "K"


class _KeyRecorder(http.server.BaseHTTPRequestHandler):
    """Accepts every close and records which key each one came with."""

    posts: list[tuple[str, str]] = []

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.posts.append((str(body.get("session_id")), str(self.headers.get("X-API-Key"))))
        self._send(200, {"ok": True, "memory_id": "m1"})

    def do_GET(self) -> None:
        self._send(200, {"rendered": "Remembra brief"})

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def key_recorder() -> Iterator[tuple[str, type[_KeyRecorder]]]:
    handler = type("Handler", (_KeyRecorder,), {"posts": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler
    finally:
        srv.shutdown()


def test_a_keyless_handoff_is_never_sent_to_a_server_configured_later(tmp_path, home):
    """Queued with no key while REMEMBRA_URL named server A: a key set up later for server B does not
    receive it; status says why it is held; a key for A sends it."""
    laptop, _ = _repo(tmp_path, "keyless-bound")
    with key_recorder() as (server_a, seen_a), key_recorder() as (server_b, seen_b):
        close = relay(
            home,
            server_a,
            "close",
            "--agent",
            "claude-code",
            "--session-id",
            "KA",
            "--cwd",
            str(laptop),
            env={"REMEMBRA_API_KEY": ""},
        )
        assert "queued" in close.stderr
        # No REMEMBRA_URL at all: bound to the local default, not to whatever comes next.
        relay(
            home,
            "",
            "close",
            "--agent",
            "gemini",
            "--session-id",
            "KD",
            "--cwd",
            str(laptop),
            env={"REMEMBRA_API_KEY": "", "REMEMBRA_URL": ""},
        )
        stored = {e.session_id: e.url for e in outbox.pending(home)}
        assert stored == {"KA": server_a, "KD": "http://localhost:8787"}

        brief_b = relay(home, server_b, "brief", "--agent", "codex", "--cwd", str(laptop))
        assert brief_b.returncode == 0 and "Remembra: sent" not in brief_b.stdout
        assert [s for s, _ in seen_b.posts] == []  # neither keyless handoff went to B
        assert "are for another server" in brief_b.stdout
        status = relay(home, server_b, "status", "--no-check")
        assert f"held: it is for {server_a}, and the key now points at {server_b}" in status.stdout
        held = json.loads(relay(home, server_b, "status", "--no-check", "--format", "json").stdout)["queue"]
        assert all(item["held"] for item in held)

        brief_a = relay(home, server_a, "brief", "--agent", "codex", "--cwd", str(laptop))
        assert "sent 1 queued handoff from claude-code" in brief_a.stdout
        assert seen_a.posts == [("KA", KEY)]
        assert [e.session_id for e in outbox.pending(home)] == ["KD"]  # still only for localhost:8787


def test_a_queued_handoff_goes_only_with_the_key_of_the_source_that_queued_it(tmp_path, home):
    """Queued with the key in ~/.claude.json: when that key is gone and another source
    (~/.remembra/credentials) has a different key for the same server, it waits; it is sent
    once ~/.claude.json has a key again, with that key."""
    laptop, _ = _repo(tmp_path, "source-bound")
    claude_key = "rem_claude_source_key_0123456789ab"
    creds_key = "rem_credentials_other_key_98765432"
    claude_json = home / ".claude.json"

    def claude_config(key: str | None, url: str) -> None:
        env = {"REMEMBRA_URL": url, **({"REMEMBRA_API_KEY": key} if key else {})}
        claude_json.write_text(json.dumps({"mcpServers": {"remembra": {"command": "remembra-mcp", "env": env}}}))

    with key_recorder() as (url, seen), fixed_status_server(503, "down") as down:
        no_env = {"REMEMBRA_API_KEY": "", "REMEMBRA_URL": ""}
        claude_config(claude_key, down)
        close = relay(home, "", "close", "--agent", "claude-code", "--session-id", "S1", "--cwd", str(laptop), env=no_env)
        assert "queued" in close.stderr
        [entry] = outbox.pending(home)
        assert entry.data["config_source"] == f"claude:{claude_json}" and entry.url == down
        # The same server comes back at another address in the test: point the entry at it, as a server that
        # was down and is up again (same URL) would be.
        data = dict(entry.data, url=url)
        entry.path.write_text(json.dumps(data))

        claude_config(None, url)  # the key was removed from Claude Code's config ...
        creds = home / ".remembra" / "credentials"
        creds.parent.mkdir(exist_ok=True)
        creds.write_text(json.dumps({"api_key": creds_key, "url": url}))  # ... and another source has one
        brief = relay(home, "", "brief", "--agent", "codex", "--cwd", str(laptop), env=no_env)
        assert brief.returncode == 0 and "Remembra: sent" not in brief.stdout
        assert seen.posts == []  # not sent with the credentials key
        status = relay(home, "", "status", "--no-check", env=no_env)
        assert f"held: its key source (claude:{claude_json}) has no key now" in status.stdout

        claude_config(claude_key, url)
        again = relay(home, "", "brief", "--agent", "codex", "--cwd", str(laptop), env=no_env)
        assert "sent 1 queued handoff from claude-code" in again.stdout
        assert ("S1", claude_key) in seen.posts and ("S1", creds_key) not in seen.posts
        assert outbox.pending(home) == []


# --- outbox unit behaviour --------------------------------------------------


def _payload(agent: str = "claude-code", session: str = "s1", **facts: Any) -> dict[str, Any]:
    return {"agent_id": agent, "session_id": session, "project_id": "p", "facts": {"branch": "main", **facts}}


def test_outbox_bounds_age_and_size(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ENTRIES", 5)
    for n in range(8):
        assert outbox.enqueue(tmp_path, _payload(session=f"s{n}"), url="http://x", config_source="env", error="down")
    names = sorted(e.session_id for e in outbox.pending(tmp_path))
    assert names == ["s3", "s4", "s5", "s6", "s7"]  # the oldest were dropped
    assert "queue full (5)" in outbox.log_path(tmp_path).read_text()

    old = outbox.pending(tmp_path)[0]
    data = json.loads(old.path.read_text())
    data["queued_ts"] = time.time() - outbox.MAX_AGE_SECONDS - 60
    old.path.write_text(json.dumps(data))
    outbox.prune(tmp_path)
    assert old.session_id not in {e.session_id for e in outbox.pending(tmp_path)}

    huge = _payload(session="big", commands=[{"cmd": "x" * 5000, "exit_code": 0}] * 200)
    path = outbox.enqueue(tmp_path, huge, url="http://x", config_source="env", error="down")
    assert path is not None and path.stat().st_size <= outbox.MAX_ENTRY_BYTES
    kept = json.loads(path.read_text())["payload"]["facts"]
    assert len(kept["commands"]) == 40 and "outbox-trimmed" in kept["incomplete"]
    assert outbox.enqueue(tmp_path, {"agent_id": "a", "facts": {}}, url=None, config_source=None, error="x") is None


def test_outbox_claims_are_exclusive_and_a_newer_entry_wins(tmp_path):
    outbox.enqueue(tmp_path, _payload(notes="first"), url="http://x", config_source="env", error="down")
    [entry] = outbox.pending(tmp_path)
    claimed = outbox.claim(entry)
    assert claimed is not None and outbox.claim(entry) is None  # a second sender cannot take it
    assert outbox.pending(tmp_path) == []
    # While it is being sent, a newer close of the same session is queued: the send fails, the newer one stays.
    outbox.enqueue(tmp_path, _payload(notes="second"), url="http://x", config_source="env", error="down")
    outbox.finish(entry, claimed, sent=False, error="timeout")
    [left] = outbox.pending(tmp_path)
    assert left.payload["facts"]["notes"] == "second" and not claimed.exists()

    # A sender that died mid-send: its claim goes back after STALE_CLAIM_SECONDS.
    stale = outbox.claim(left)
    assert stale is not None
    past = time.time() - outbox.STALE_CLAIM_SECONDS - 5
    os.utime(stale, (past, past))
    [back] = outbox.pending(tmp_path)
    assert back.payload["facts"]["notes"] == "second"


def test_outbox_sets_aside_a_corrupt_entry_and_keeps_names_safe(tmp_path):
    path = outbox.entry_path(tmp_path, "claude-code", "../../etc/passwd")
    assert path.parent == outbox.outbox_dir(tmp_path) and "/" not in path.name.replace(".json", "")
    assert outbox.entry_path(tmp_path, "a", "b/c") != outbox.entry_path(tmp_path, "a", "b_c")
    outbox.outbox_dir(tmp_path).mkdir(parents=True)
    (outbox.outbox_dir(tmp_path) / "broken.json").write_text("{not json")
    assert outbox.pending(tmp_path) == []
    assert (outbox.outbox_dir(tmp_path) / "broken.json.corrupt").exists()


def test_outbox_log_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "LOG_MAX_BYTES", 200)
    for n in range(20):
        outbox.log(tmp_path, f"line {n} " + "x" * 30)
    assert outbox.log_path(tmp_path).stat().st_size < 400
    assert outbox.log_path(tmp_path).with_name("relay.log.1").exists()


def test_clean_url_drops_credentials():
    assert outbox.clean_url("https://user:pw@API.remembra.dev/?q=1#x") == "https://api.remembra.dev"
    assert outbox.clean_url("http://127.0.0.1:8787/") == "http://127.0.0.1:8787"
    assert outbox.clean_url(None) is None and outbox.clean_url("") is None


# ---------------------------------------------------------------------------
# R-32: disconnect
# ---------------------------------------------------------------------------

PRE_CONNECT: dict[str, str | None] = {
    "claude-code": json.dumps(
        {
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}]},
        },
        indent=2,
    ),
    "codex": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]}}),
    "cursor": json.dumps({"version": 1, "hooks": {"afterFileEdit": [{"command": "./format.sh"}]}}),
    "gemini": json.dumps({"theme": "dark"}),
    "qwen": None,  # no config file before connect
    "kimi": 'default_model = "k2"\n\n[providers.moonshot]\nbase_url = "https://api.moonshot.ai/v1"\n',
}


def _parsed(name: str, text: str | None) -> Any:
    if text is None:
        return None
    return tomllib.loads(text) if name == "kimi" else json.loads(text)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_disconnect_restores_each_agent_to_its_pre_connect_settings(home, name):
    path = REGISTRY[name].spec.config_path(home)
    before = PRE_CONNECT[name]
    if before is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(before)
    connect = relay(home, "http://x", "connect", "--agent", name, "--apply", "--include-unverified", "--relay-command", RELAY_CMD)
    assert connect.returncode == 0, connect.stderr
    connected = path.read_text()
    assert RELAY_CMD in connected

    dry = relay(home, "http://x", "disconnect", "--agent", name)
    assert dry.returncode == 0, dry.stderr
    assert "(dry run: re-run with --apply" in dry.stdout
    assert path.read_text() == connected  # a dry run changes nothing

    applied = relay(home, "http://x", "disconnect", "--agent", name, "--apply")
    assert applied.returncode == 0, applied.stderr
    if before is None:
        assert not path.exists()  # the file only ever held our hooks
    else:
        assert _parsed(name, path.read_text()) == _parsed(name, before)
    backups = list(path.parent.glob(f"{path.name}.bak-relay-*"))
    assert any(b.read_text() == connected for b in backups)
    assert all(_mode(b) == 0o600 for b in backups)
    again = relay(home, "http://x", "disconnect", "--agent", name, "--apply")
    assert "no relay hooks" in again.stdout


def test_disconnect_without_apply_touches_no_file(home, tmp_path):
    md = tmp_path / "AGENTS.md"
    md.write_text("# Rules\n")
    for name in REGISTRY:
        path = REGISTRY[name].spec.config_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
    agents = [arg for name in REGISTRY for arg in ("--agent", name)]
    connect = relay(
        home,
        "http://x",
        "connect",
        *agents,
        "--apply",
        "--include-unverified",
        "--agents-md",
        str(md),
        "--relay-command",
        RELAY_CMD,
    )
    assert connect.returncode == 0, connect.stderr
    files = [REGISTRY[n].spec.config_path(home) for n in REGISTRY] + [md]
    snapshot = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files}
    time.sleep(0.02)
    dry = relay(home, "http://x", "disconnect", "--agents-md", str(md))
    assert dry.returncode == 0, dry.stderr
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files} == snapshot
    assert "pipx uninstall remembra" in dry.stdout and "remembra-install --remove --all --apply" in dry.stdout

    applied = relay(home, "http://x", "disconnect", "--agents-md", str(md), "--apply")
    assert applied.returncode == 0
    assert md.read_text() == "# Rules\n"
    assert not any(RELAY_CMD in p.read_text() for p in files if p.exists())


def test_a_handoff_queued_for_another_server_is_not_sent_here(tmp_path, home, server):
    outbox.enqueue(home, _payload(session="elsewhere"), url="https://other.example", config_source="env", error="down")
    brief = relay(home, server, "brief", "--agent", "codex", "--cwd", str(tmp_path))
    assert "1 queued handoff(s) are for another server (https://other.example)" in brief.stdout
    assert "Remembra: sent" not in brief.stdout
    [left] = outbox.pending(home)
    assert left.session_id == "elsewhere" and left.attempts == 1
