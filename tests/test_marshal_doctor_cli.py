"""``remembra-relay doctor`` end to end: the real CLI in a subprocess, real HTTP, the production routes.

A uvicorn server runs the relay API (the ``test_relay_cli_e2e`` harness). Claude Code closes a
session through the real ``close``, Codex reads a brief through the real ``brief`` (recording a
pickup), and the doctor reads both back with GETs. Two tiny stub servers answer 401 and a
Cloudflare-style HTML 403. The CLI runs with an isolated HOME and no key but the test's own.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from remembra.marshal import commands
from tests.marshal_fixtures import CLOUDFLARE_PAGE, KEY, FakeHome
from tests.relay_fixtures import GIT_ENV, make_remote_and_clones
from tests.test_relay_cli_e2e import server  # noqa: F401  (module-scoped uvicorn fixture)

SRC = str(Path(__file__).resolve().parents[1] / "src")
FINDING_KEYS = {"id", "severity", "agent", "what", "evidence", "inferred", "fix", "then", "doc", "caveat", "marker", "proven"}
TOP_KEYS = {
    "version",
    "ruleset",
    "generated_at",
    "repo",
    "config",
    "reads",
    "agents",
    "findings",
    "unchecked",
    "things_to_do",
    "to_watch",
    "exit_code",
    "changed_nothing",
}


def cli(fh: FakeHome, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": f"{fh.bin}:{os.environ.get('PATH', '')}",
        "HOME": str(fh.home),
        "PYTHONPATH": SRC,
        **GIT_ENV,
    }
    base.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", *args],
        capture_output=True,
        text=True,
        env=base,
        cwd=str(cwd or fh.root),
        timeout=90,
    )


def _stub(status: int, body: str, content_type: str, headers: dict[str, str] | None = None) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        seen: list[tuple[str, str]] = []

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            Handler.seen.append(("GET", self.path))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self) -> None:  # noqa: N802
            Handler.seen.append(("POST", self.path))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        assert all(method == "GET" for method, _ in Handler.seen), Handler.seen


def _check_json(data: dict) -> None:
    assert set(data) == TOP_KEYS, set(data) ^ TOP_KEYS
    assert data["changed_nothing"] is True
    for finding in data["findings"]:
        assert set(finding) == FINDING_KEYS, finding
        assert finding["severity"] in ("blocker", "warn", "info")
        assert finding["marker"] in ("[!!]", "[??]", "")
        fix = finding["fix"]
        if fix and fix["command"]:
            assert commands.is_allowed(fix["command"]), fix["command"]
            assert fix["runs_where"] in ("agent_ok", "user_terminal")
    for read in data["reads"]:
        assert set(read) == {"what", "result", "ms", "ok"}


def _check_text(text: str) -> None:
    assert "\033[" not in text
    assert text.rstrip().endswith("Nothing was changed.")
    for line in text.splitlines():
        assert len(line) <= 76 or commands.is_allowed(line.replace("┊", "").strip()), line


def test_doctor_reads_a_real_trail(server: str, tmp_path: Path) -> None:  # noqa: F811
    fh = FakeHome(tmp_path)
    fh.hooks("claude-code")
    fh.hooks("codex")
    fh.trust_codex()
    env = {"REMEMBRA_URL": server, "REMEMBRA_API_KEY": KEY}
    _, clones = make_remote_and_clones(tmp_path, ("laptop",))
    repo = clones["laptop"]

    closed = cli(fh, "close", "--agent", "claude-code", "--session-id", "cc-1", "--cwd", str(repo), "--next", "ship it", env=env)
    assert closed.returncode == 0, closed.stderr
    brief = cli(fh, "brief", "--agent", "codex", "--session-id", "cx-1", "--cwd", str(repo), env=env)
    assert brief.returncode == 0 and "Last session: claude-code" in brief.stdout, brief.stdout

    out = cli(fh, "doctor", "--format", "json", env=env, cwd=repo)
    data = json.loads(out.stdout)
    _check_json(data)
    assert data["repo"] == {"name": "widget", "branch": "main"}
    reads = {r["what"]: r["result"] for r in data["reads"]}
    assert reads["key"].startswith("accepted by 127.0.0.1:") and "from env" in reads["key"]
    agents = {a["name"]: a for a in data["agents"]}
    assert agents["claude-code"]["trail"]["handoffs_7d"] == 1
    assert agents["codex"]["trail"]["pickups"] == 1 and agents["codex"]["trail"]["entries_7d"] == 0
    # Codex read a brief with its hooks trusted but no handoff from it arrived: proven, not guessed.
    finding = next(f for f in data["findings"] if f["id"] == "PICKS_UP_NEVER_CLOSES")
    assert finding["agent"] == "codex" and finding["proven"] is True
    assert finding["what"] == "Codex read 1 brief but never handed off: its close hasn't reached Remembra."
    assert out.returncode == 1 == data["exit_code"]

    text = cli(fh, "doctor", env={**env, "NO_COLOR": "1"}, cwd=repo)
    _check_text(text.stdout)
    assert text.stdout.startswith("remembra-relay doctor · widget (main) · remembra ")
    assert "◆ last handoff just now · picked up by codex just now" in text.stdout
    colored = cli(fh, "doctor", "--color", "always", env=env, cwd=repo)
    assert "\033[38;5;208m" in colored.stdout  # ANSI 208 without COLORTERM=truecolor
    truecolor = cli(fh, "doctor", "--color", "always", env={**env, "COLORTERM": "truecolor"}, cwd=repo)
    assert "\033[38;2;255;107;43m" in truecolor.stdout

    # Codex closes too: nothing left to do, exit 0.
    closed = cli(fh, "close", "--agent", "codex", "--session-id", "cx-1", "--cwd", str(repo), "--next", "review it", env=env)
    assert closed.returncode == 0, closed.stderr
    clean = cli(fh, "doctor", "--agent", "claude-code", "--agent", "codex", env=env, cwd=repo)
    assert clean.returncode == 0, clean.stdout
    assert "Nothing to do." in clean.stdout
    _check_text(clean.stdout)


def test_a_close_fixed_before_a_brief_is_not_reported_as_failing(server: str, tmp_path: Path) -> None:  # noqa: F811
    """A failed close, a close that works, then a brief: status.json only keeps the brief, the trail keeps the handoff."""
    from remembra.relay import outbox

    fh = FakeHome(tmp_path)
    fh.hooks("claude-code")
    env = {"REMEMBRA_URL": server, "REMEMBRA_API_KEY": KEY}
    _, clones = make_remote_and_clones(tmp_path, ("laptop",))
    repo = clones["laptop"]
    # What `close` records when the server rejects the body (HTTP 422: not queued, not retried).
    outbox.record(fh.home, agent_id="claude-code", command="close", ok=False, error="HTTP 422: bad body", http_status=422)
    failing = json.loads(cli(fh, "doctor", "--agent", "claude-code", "--format", "json", env=env, cwd=repo).stdout)
    found = [f for f in failing["findings"] if f["id"] == "CLOSE_FAILING"]
    assert len(found) == 1 and found[0]["proven"] is True and found[0]["what"].endswith("no close has worked since.")

    closed = cli(fh, "close", "--agent", "claude-code", "--session-id", "fix-1", "--cwd", str(repo), "--next", "go", env=env)
    assert closed.returncode == 0, closed.stderr
    brief = cli(fh, "brief", "--agent", "claude-code", "--session-id", "fix-2", "--cwd", str(repo), env=env)
    assert brief.returncode == 0, brief.stderr
    slot = json.loads((fh.home / ".remembra" / "relay" / "status.json").read_text())["agents"]["claude-code"]
    assert slot["last_success"]["command"] == "brief" and slot["last_failure"]["command"] == "close"

    out = cli(fh, "doctor", "--agent", "claude-code", env={**env, "NO_COLOR": "1"}, cwd=repo)
    assert "last close failed" not in out.stdout and "Nothing to do." in out.stdout, out.stdout
    assert "close failed just now (HTTP 422)" in out.stdout  # the station still shows what status.json holds
    _check_text(out.stdout)
    # Without the trail the brief hides whether a close worked: a note, never "no close has worked since".
    offline = cli(fh, "doctor", "--agent", "claude-code", "--no-server", "--format", "json", env=env, cwd=repo)
    note = next(f for f in json.loads(offline.stdout)["findings"] if f["id"] == "CLOSE_FAILING")
    assert (note["severity"], note["marker"]) == ("info", "[??]") and "no close has worked since" not in note["what"]
    assert offline.returncode == 0


def test_an_unverified_adapter_left_out_by_connect_is_a_note(tmp_path: Path) -> None:
    """setup.md step 7: connect --apply leaves Cursor (unverified) out unless asked. The doctor then notes it, never [!!]."""
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.install("claude", "codex", "cursor-agent")
    for folder in (".claude", ".codex", ".cursor"):
        (fh.home / folder).mkdir()
    path = {"PATH": f"{fh.bin}:/usr/bin:/bin"}  # only the agents installed here, not this machine's own
    applied = cli(fh, "connect", "--apply", "--relay-command", fh.relay, env=path)
    assert applied.returncode == 0, applied.stderr
    assert "skipped: unverified adapter" in applied.stdout
    fh.trust_codex()  # what /hooks in Codex writes
    out = cli(fh, "doctor", "--no-server", "--format", "json", env=path)
    data = json.loads(out.stdout)
    _check_json(data)
    note = next(f for f in data["findings"] if f["id"] == "UNVERIFIED_NOT_WRITTEN")
    assert (note["agent"], note["severity"], note["marker"]) == ("cursor", "info", "")
    assert not any("dry run" in e for e in note["evidence"]), note["evidence"]
    assert "connect --apply leaves unverified adapters out unless you add --include-unverified" in note["evidence"]
    assert note["fix"]["command"] == "remembra-relay connect --apply --agent cursor --include-unverified"
    assert (out.returncode, data["exit_code"], data["things_to_do"]) == (0, 0, 0)
    text = cli(fh, "doctor", "--no-server", env={**path, "NO_COLOR": "1"})
    assert text.returncode == 0 and "Nothing to do." in text.stdout and "[!!]" not in text.stdout, text.stdout
    assert "○ cursor       hooks left out · unverified" in text.stdout
    _check_text(text.stdout)


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    bad = cli(fh, "doctor", "--agent", "notepad")
    assert bad.returncode == 2 and "invalid choice: 'notepad'" in bad.stderr
    assert cli(fh, "doctor", "--color", "sometimes").returncode == 2
    assert cli(fh, "doctor", "--format", "yaml").returncode == 2


def test_no_key_offline(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.hooks("claude-code")
    out = cli(fh, "doctor", "--no-server")
    assert out.returncode == 1
    assert "No API key on this machine" in out.stdout and "remembra-install --all" in out.stdout
    _check_text(out.stdout)


@pytest.fixture()
def rejecting() -> Iterator[str]:
    yield from _stub(401, json.dumps({"detail": "Invalid API key"}), "application/json")


@pytest.fixture()
def firewalled() -> Iterator[str]:
    yield from _stub(403, CLOUDFLARE_PAGE, "text/html", {"Server": "cloudflare", "CF-RAY": "8c1f2e3d4a5b6c7d-IAD"})


@pytest.fixture()
def redirecting() -> Iterator[str]:
    """http:// in front of an https-only proxy: every path answers 301 to the same path over https."""
    yield from _stub(301, "", "text/plain", {"Location": "https://api.remembra.test/api/v1/trail/summary?days=7"})


def test_a_redirect_over_http_is_a_wrong_url_not_a_working_key(redirecting: str, tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials(url=redirecting)
    fh.hooks("claude-code")
    # The hooks themselves don't follow the redirect: the brief gets no answer it can read.
    brief = cli(fh, "brief", "--agent", "claude-code", "--cwd", str(fh.root))
    assert "brief failed" in brief.stderr, brief.stderr
    out = cli(fh, "doctor", "--format", "json")
    data = json.loads(out.stdout)
    _check_json(data)
    reads = {r["what"]: r for r in data["reads"]}
    assert reads["key"]["ok"] is False and "accepted" not in reads["key"]["result"]
    finding = next(f for f in data["findings"] if f["id"] == "SERVER_WRONG_URL")
    assert finding["marker"] == "[!!]" and "the hooks don't follow redirects" in finding["what"]
    assert finding["fix"]["command"] == "remembra-install --all --url https://api.remembra.test"
    assert finding["fix"]["runs_where"] == "user_terminal"
    assert not [f for f in data["findings"] if f["id"] in ("HOOKS_NOT_FIRING", "NOTHING_WAITING")]
    assert out.returncode == 1 and KEY not in out.stdout
    _check_text(cli(fh, "doctor", env={"NO_COLOR": "1"}).stdout)


def test_a_rejected_key_over_http(rejecting: str, tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials(url=rejecting)
    fh.hooks("claude-code")
    out = cli(fh, "doctor", "--format", "json")
    data = json.loads(out.stdout)
    _check_json(data)
    finding = next(f for f in data["findings"] if f["id"] == "KEY_REJECTED")
    assert finding["fix"]["command"] == f"remembra-install --all --url {rejecting}"
    assert finding["fix"]["runs_where"] == "user_terminal"
    assert out.returncode == 1 and KEY not in out.stdout


def test_the_firewall_over_http(firewalled: str, tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials(url=firewalled)
    fh.hooks("claude-code")
    out = cli(fh, "doctor")
    assert "Blocked by the server's firewall" in out.stdout and "Cloudflare Ray ID 8c1f2e3d4a5b6c7d" in out.stdout
    assert "KEY_REFUSED" not in out.stdout and "refused the key" not in out.stdout
    _check_text(out.stdout)
