"""The crew E2E world (WP-15, spec §13.3): a real server, real crewd, real hooks, fake model.

Everything runs on this machine against temporary state only: a temp ``HOME`` (so ``connect``
writes ``<tmp>/home/.claude/settings.json`` and ``<tmp>/home/.remembra/crew``, never the real
ones), a temp git config, temp repositories and a server process bound to 127.0.0.1.

* :class:`ServerProc` runs :mod:`tests.crew.e2e.server` (the full crew-mode app, auth on).
* :class:`WebhookReceiver` is the "Clawdbot bridge": it answers the signed challenge and records
  every signed delivery.
* :meth:`World.connect` runs the real ``remembra-crew connect --apply --yes`` on a pseudo-terminal
  (the consent answer), installing the §8.2 Claude Code hooks, the vendored gate and the git gates.
* :class:`FakeAgent` is one "Claude Code" process (:mod:`tests.crew.e2e.fake_claude`, started
  through a symlink named ``claude``). It runs the installed hook commands with payloads rebuilt
  from the S0 captures (``tests/crew/fixtures/captures/claude-code-2.1.168``).
* :class:`CrewFeed` is a dashboard-style subscriber on the real ``/ws`` (JWT, crew topic).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import pty
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from remembra.crew import schemas as S
from remembra.crew.notify import SIGNATURE_HEADER, verify_signature
from remembra.relay.crew.gate import Layout, read_json, session_key

ROOT = Path(__file__).resolve().parents[3]
PY = sys.executable
CAPTURES = ROOT / "tests" / "crew" / "fixtures" / "captures" / "claude-code-2.1.168"
FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
DASHBOARD = ROOT / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"
DASHBOARD_E2E = Path(__file__).with_name("dashboard")
WEBHOOK_URL = "https://hooks.e2e.test/remembra"

ZONES_YML = """version: 1
zones:
  pos:
    title: POS section
    include: [src/app/pos/**]
    mode: exclusive
  reports:
    title: Reports
    include: [src/app/reports/**]
commons:
  package.json: plain
  supabase/migrations/**: append_only
"""

PACKAGE_JSON = {
    "name": "yaadbooks-e2e",
    "private": True,
    "scripts": {"test": "node -e \"console.log('Tests:       3 passed, 3 total')\""},
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(fn: Callable[[], Any], timeout: float = 15.0, interval: float = 0.2) -> Any:
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= end:
            return value
        time.sleep(interval)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(res.stdout.strip()) and not res.stdout.strip().startswith("Z")


# ---------------------------------------------------------------------------
# Recorded payloads (S0)
# ---------------------------------------------------------------------------


def capture(mode: str, scenario: str, event: str) -> dict[str, Any]:
    """The recorded stdin payload of ``event`` in one S0 run (sanitised; placeholders replaced by callers)."""
    folder = CAPTURES / mode / scenario
    for path in sorted(folder.glob(f"*-{event}.json")):
        return dict(json.loads(path.read_text(encoding="utf-8"))["payload"])
    raise FileNotFoundError(f"no {event} capture in {folder}")


class Payloads:
    """Hook payloads for one session, rebuilt from the S0 captures with this session's values."""

    def __init__(self, session_id: str, cwd: Path, transcript: Path) -> None:
        self.session_id = session_id
        self.cwd = str(cwd)
        self.transcript = str(transcript)
        self.base = {"session_id": session_id, "cwd": self.cwd, "transcript_path": self.transcript}

    def _fill(self, recorded: dict[str, Any], **extra: Any) -> dict[str, Any]:
        out = copy.deepcopy(recorded)
        out.update(self.base)
        out.update(extra)
        return out

    def session_start(self, source: str = "startup") -> dict[str, Any]:
        return self._fill(capture("mock", "stopfailure_billing", "SessionStart"), source=source)

    def prompt(self, text: str) -> dict[str, Any]:
        return self._fill(capture("mock", "context", "UserPromptSubmit"), prompt=text, permission_mode="acceptEdits")

    def pre_tool(self, tool: str, tool_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        tid = "toolu_e2e_" + uuid.uuid4().hex[:20]
        pre = self._fill(
            capture("mock", "context", "PreToolUse"),
            tool_name=tool,
            tool_input=tool_input,
            tool_use_id=tid,
            permission_mode="acceptEdits",
        )
        post = self._fill(
            capture("mock", "context", "PostToolUse"),
            tool_name=tool,
            tool_input=tool_input,
            tool_use_id=tid,
            permission_mode="acceptEdits",
        )
        post.pop("tool_response", None)
        return pre, post

    def stop(self) -> dict[str, Any]:
        return self._fill(capture("mock", "context", "Stop"), stop_hook_active=False)

    def stop_failure(self, error: str, message: str) -> dict[str, Any]:
        recorded = capture("mock", "stopfailure_billing", "StopFailure")
        return self._fill(recorded, error=error, last_assistant_message=message)

    def session_end(self, reason: str = "other") -> dict[str, Any]:
        return self._fill(capture("mock", "stopfailure_billing", "SessionEnd"), reason=reason)


# ---------------------------------------------------------------------------
# Server and webhook receiver
# ---------------------------------------------------------------------------


class ServerProc:
    def __init__(self, workdir: Path, *, webhook_forward: str | None = None) -> None:
        self.workdir = workdir
        self.port = free_port()
        self.webhook_forward = webhook_forward
        self.proc: subprocess.Popen[str] | None = None
        self.info: dict[str, Any] = {}
        self.log_path = workdir / "server.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> ServerProc:
        self.workdir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "CLAUDE"))}
        env.update({"PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}", "REMEMBRA_TYPESAFE_MODE": "off"})
        env.pop("TYPESAFE_API_KEY", None)
        argv = [PY, "-m", "tests.crew.e2e.server", "--workdir", str(self.workdir), "--port", str(self.port)]
        if self.webhook_forward:
            argv += ["--webhook-forward", self.webhook_forward]
        self.proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        log = self.log_path.open("w", encoding="utf-8")
        deadline = time.monotonic() + 90
        assert self.proc.stdout is not None
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith('{"port"'):
                self.info = json.loads(line)
                break
            log.write(line)
        if not self.info:
            self.stop()
            log.close()
            raise RuntimeError(f"e2e server did not start; see {self.log_path}")
        stream = self.proc.stdout

        def drain() -> None:
            for line in stream:
                log.write(line)
                log.flush()
            log.close()

        threading.Thread(target=drain, daemon=True).start()
        return self

    @property
    def key(self) -> str:
        return str(self.info["key"])

    @property
    def jwt(self) -> str:
        return str(self.info["jwt"])

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class WebhookReceiver:
    """A local HTTP endpoint the server's real webhook sender forwards to (see ``server.ForwardTransport``)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.secret: str | None = None
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                entry = {"headers": dict(self.headers), "raw": raw, "body": body, "at": time.time(), "path": self.path}
                with receiver.lock:
                    receiver.requests.append(entry)
                out = b"{}"
                if isinstance(body, dict) and body.get("type") == "crew.notification.challenge":
                    out = json.dumps({"challenge": body.get("challenge")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def deliveries(self) -> list[dict[str, Any]]:
        """Signed ``crew.notification`` deliveries whose signature verifies with the registered secret."""
        with self.lock:
            items = list(self.requests)
        out = []
        for r in items:
            body = r["body"]
            if not isinstance(body, dict) or body.get("type") != "crew.notification":
                continue
            header = r["headers"].get(SIGNATURE_HEADER) or ""
            r["verified"] = bool(self.secret) and verify_signature(str(self.secret), r["raw"], header)
            out.append(r)
        return out

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# Dashboard-style feed on the real /ws
# ---------------------------------------------------------------------------


class CrewFeed:
    """Subscribes to one crew over ``/ws`` with the owner's JWT; records every frame with its arrival time."""

    def __init__(self, port: int, jwt: str, crew_id: str, *, since_seq: int = 0) -> None:
        self.port = port
        self.jwt = jwt
        self.crew_id = crew_id
        self.since_seq = since_seq
        self.frames: list[dict[str, Any]] = []
        self.arrivals: dict[int, float] = {}
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.error: str | None = None
        self.subscribed_at = 0.0
        self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self.thread.start()
        if not self.ready.wait(15):
            raise RuntimeError(f"crew feed did not subscribe: {self.error}")

    async def _run(self) -> None:
        import websockets

        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{self.port}/ws", additional_headers={"Authorization": f"Bearer {self.jwt}"}
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "channel": "crew",
                            "crew_id": self.crew_id,
                            "since_seq": self.since_seq,
                            "topics": ["crew"],
                        }
                    )
                )
                while not self.stop_flag.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    now = time.time()
                    if not isinstance(msg, dict):
                        continue
                    self.frames.append(msg)
                    if msg.get("type") == "crew.subscribed":
                        self.subscribed_at = now
                        self.ready.set()
                    if msg.get("type") == "crew.error":
                        self.error = json.dumps(msg)
                        self.ready.set()
                    if msg.get("type") == "crew.event":
                        seq = int(msg["data"]["seq"])
                        self.arrivals.setdefault(seq, now)
        except Exception as e:  # surfaced through .error
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()

    def events(self) -> list[dict[str, Any]]:
        return [f["data"] for f in self.frames if f.get("type") == "crew.event"]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events()]

    def presence(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == "presence"]

    def close(self) -> None:
        self.stop_flag.set()
        self.thread.join(timeout=5)


class DashboardObserver:
    """The dashboard's own data layer and view models following the crew live (``dashboard/crew.e2e.test.ts``).

    Started under the dashboard's vitest as soon as the crew exists; it signals ``ready.json`` once its
    store is live, and finishes (reload comparison, report) when :meth:`finish` writes ``done``.
    """

    def __init__(self, server: ServerProc, crew_id: str, workdir: Path) -> None:
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.out = self.dir / "vitest.out"
        env = {**os.environ, "CI": "1", "CREW_E2E_URL": server.url, "CREW_E2E_JWT": server.jwt, "CREW_E2E_CREW": crew_id}
        env["CREW_E2E_DIR"] = str(self.dir)
        self.log = self.out.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(VITEST), "run", "--dir", str(DASHBOARD_E2E), "crew.e2e.test.ts"],
            cwd=str(DASHBOARD),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if not wait_for(lambda: (self.dir / "ready.json").exists() or self.proc.poll() is not None, timeout=120):
            raise RuntimeError("the dashboard observer did not start")
        if self.proc.poll() is not None:
            raise RuntimeError(f"the dashboard observer exited: {self.out.read_text()[-3000:]}")

    def finish(self, last_seq: int, timeout: float = 120) -> dict[str, Any]:
        (self.dir / "done").write_text(json.dumps({"last_seq": last_seq}))
        try:
            self.proc.wait(timeout=timeout)
        finally:
            self.log.close()
        text = self.out.read_text(encoding="utf-8")
        assert self.proc.returncode == 0, text[-6000:]
        assert "1 passed" in text, text[-3000:]
        return dict(json.loads((self.dir / "report.json").read_text(encoding="utf-8")))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# A fake Claude Code process
# ---------------------------------------------------------------------------


class FakeAgent:
    """One model-free Claude Code session in one checkout (see :mod:`tests.crew.e2e.fake_claude`)."""

    def __init__(self, world: World, name: str, cwd: Path, *, session_id: str | None = None) -> None:
        self.world = world
        self.name = name
        self.cwd = cwd
        self.session_id = session_id or str(uuid.uuid4())
        self.transcript = world.tmp / "transcripts" / f"{self.session_id}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("")
        self.env_file = world.tmp / "envfiles" / f"{name}.env"
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text("")
        self.p = Payloads(self.session_id, cwd, self.transcript)
        self.proc = subprocess.Popen(
            [
                str(world.claude_bin),
                str(FAKE_CLAUDE),
                "--settings",
                str(world.settings_file),
                "--cwd",
                str(cwd),
                "--env-file",
                str(self.env_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=world.agent_env(),
            cwd=str(cwd),
        )
        self._n = 0
        self.pid = int(self.request("ping")["pid"])
        self.brief: str = ""
        self.killed = False

    @property
    def key(self) -> str:
        return session_key("claude-code", self.session_id)

    def local_session(self) -> dict[str, Any]:
        return read_json(self.world.layout.session_file(self.key)) or {}

    def request(self, op: str, **kw: Any) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._n += 1
        self.proc.stdin.write(json.dumps({"op": op, "id": self._n, **kw}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"agent {self.name} exited")
        out = json.loads(line)
        if out.get("error"):
            raise RuntimeError(f"agent {self.name}: {out['error']}")
        return dict(out)

    def hook(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.request("hook", event=event, payload=payload)["hooks"])

    # -- the session as Claude Code runs it --------------------------------------------------
    def start(self, source: str = "startup") -> str:
        hooks = self.hook("SessionStart", self.p.session_start(source))
        [h] = hooks
        assert h["exit"] == 0, h
        assert S.validate_hook_stdout("SessionStart", h["stdout"]) == [], h["stdout"]
        self.brief = json.loads(h["stdout"])["hookSpecificOutput"]["additionalContext"]
        return self.brief

    def prompt(self, text: str) -> list[dict[str, Any]]:
        return self.hook("UserPromptSubmit", self.p.prompt(text))

    def tool(self, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        pre, post = self.p.pre_tool(tool, tool_input)
        return self.request("tool", pre=pre, post=post)

    def path(self, rel: str) -> str:
        return str(self.cwd / rel)

    def write(self, rel: str, content: str) -> dict[str, Any]:
        path = rel if rel.startswith("/") else self.path(rel)
        return self.tool("Write", {"file_path": path, "content": content})

    def edit(self, rel: str, old: str, new: str) -> dict[str, Any]:
        path = rel if rel.startswith("/") else self.path(rel)
        return self.tool("Edit", {"file_path": path, "old_string": old, "new_string": new})

    def bash(self, command: str) -> dict[str, Any]:
        return self.tool("Bash", {"command": command, "description": "e2e"})

    def mcp(self, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """An MCP tool call as Claude Code hooks see it (the tool itself is not run: only the gate matters)."""
        pre, _post = self.p.pre_tool(tool, tool_input)
        hooks = self.hook("PreToolUse", pre)
        for h in hooks:
            text = (h.get("stdout") or "").strip()
            if text:
                out = json.loads(text).get("hookSpecificOutput") or {}
                if out.get("permissionDecision") == "deny":
                    return {"denied": True, "reason": out.get("permissionDecisionReason"), "hooks": hooks}
        return {"denied": False, "hooks": hooks}

    def stop(self) -> list[dict[str, Any]]:
        return self.hook("Stop", self.p.stop())

    def stop_failure(self, error: str, message: str) -> list[dict[str, Any]]:
        return self.hook("StopFailure", self.p.stop_failure(error, message))

    def end(self, reason: str = "other") -> list[dict[str, Any]]:
        return self.hook("SessionEnd", self.p.session_end(reason))

    def kill(self) -> None:
        """``kill -9`` of the agent process (credits gone, laptop yanked, crash)."""
        self.killed = True
        with_suppress = self.proc.poll() is None
        if with_suppress:
            os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.request("exit")
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


def git(cwd: Path | str, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=False)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed ({res.returncode}): {res.stdout}{res.stderr}")
    return res.stdout.strip()


class World:
    """Temp HOME, repos, server, receiver and installed hooks for one E2E run."""

    def __init__(self, tmp: Path, *, zones: str | None = ZONES_YML) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.layout = Layout(self.home)
        self.bin = tmp / "bin"
        self.bin.mkdir(exist_ok=True)
        self.claude_bin = self.bin / "claude"
        base_python = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
        self.claude_bin.symlink_to(base_python)
        shim = self.bin / "remembra-crew"
        shim.write_text(f'#!/bin/sh\nexec {PY} -m remembra.relay.crew.cli "$@"\n')
        shim.chmod(0o755)
        self.gitconfig = tmp / "gitconfig"
        self.gitconfig.write_text(
            "[user]\n\tname = E2E\n\temail = e2e@example.invalid\n[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgsign = false\n[advice]\n\tdetachedHead = false\n"
        )
        self.settings_file = self.home / ".claude" / "settings.json"
        self.server: ServerProc | None = None
        self.receiver: WebhookReceiver | None = None
        self.agents: list[FakeAgent] = []
        self.feeds: list[CrewFeed] = []
        self.closers: list[Callable[[], None]] = []
        self.zones = zones
        self.origin = tmp / "remotes" / "yaadbooks-e2e.git"
        self.repo = tmp / "yaadbooks-e2e"

    # -- environment -------------------------------------------------------------------------
    def base_env(self) -> dict[str, str]:
        drop = ("REMEMBRA_", "CLAUDE", "GIT_", "HUSKY", "LEFTHOOK", "TYPESAFE")
        env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
        node_dir = os.path.dirname(shutil.which("npm") or shutil.which("node") or "/usr/bin/node")
        env.update(
            {
                "HOME": str(self.home),
                "PATH": os.pathsep.join([str(self.bin), node_dir, "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
                "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}",
                "GIT_CONFIG_GLOBAL": str(self.gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "REMEMBRA_TYPESAFE_MODE": "off",
            }
        )
        return env

    def agent_env(self) -> dict[str, str]:
        env = self.base_env()
        assert self.server is not None
        env.update({"REMEMBRA_URL": self.server.url, "REMEMBRA_API_KEY": self.server.key})
        return env

    # -- repos -------------------------------------------------------------------------------
    def make_repos(self, worktrees: tuple[str, ...] = ("wt-a", "wt-b", "wt-c")) -> dict[str, Path]:
        env = self.base_env()
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env=env, capture_output=True)
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main", env=env)
        files = {
            "src/app/pos/split.ts": "export const split = (total: number) => [total];\n",
            "src/app/pos/tender.ts": "export const tender = 1;\n",
            "src/app/reports/export.ts": "export const exportCsv = () => '';\n",
            "src/lib/money.ts": "export const round = (n: number) => Math.round(n * 100) / 100;\n",
            "supabase/migrations/0001_init.sql": "create table t (id int);\n",
            "package.json": json.dumps(PACKAGE_JSON, indent=2) + "\n",
            "README.md": "# yaadbooks-e2e\n",
        }
        if self.zones is not None:
            files[".remembra/zones.yml"] = self.zones
        for rel, text in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(self.repo, "add", "-A", env=env)
        git(self.repo, "commit", "-q", "-m", "init", env=env)
        git(self.repo, "remote", "add", "origin", str(self.origin), env=env)
        git(self.repo, "push", "-q", "-u", "origin", "main", env=env)
        out = {"main": self.repo.resolve()}
        for name in worktrees:
            path = self.tmp / name
            git(self.repo, "worktree", "add", "-q", "-b", f"work/{name}", str(path), "main", env=env)
            out[name] = path.resolve()
        return out

    # -- services ----------------------------------------------------------------------------
    def start_server(self) -> ServerProc:
        self.receiver = WebhookReceiver()
        self.server = ServerProc(self.tmp / "server", webhook_forward=self.receiver.url).start()
        return self.server

    def connect(self, *extra: str) -> str:
        """``remembra-crew connect --apply --yes`` on a pseudo-terminal, HOME = the temp home."""
        argv = [
            PY,
            "-m",
            "remembra.relay.crew.cli",
            "connect",
            "--apply",
            "--yes",
            "--no-service",
            "--git-hooks",
            "--repo",
            str(self.repo),
            "--python",
            PY,
            "--crew-command",
            str(self.bin / "remembra-crew"),
            "--crewd-command",
            f"{PY} -m remembra.relay.crew.crewd",
            *extra,
        ]
        master, slave = pty.openpty()
        try:
            res = subprocess.run(
                argv, stdin=slave, capture_output=True, text=True, env=self.base_env(), cwd=str(self.repo), timeout=120
            )
        finally:
            os.close(slave)
            os.close(master)
        assert res.returncode == 0, res.stdout + res.stderr
        assert self.settings_file.exists(), res.stdout
        return res.stdout

    def agent(self, name: str, cwd: Path, **kw: Any) -> FakeAgent:
        a = FakeAgent(self, name, cwd, **kw)
        self.agents.append(a)
        return a

    def feed(self, crew_id: str, since_seq: int = 0) -> CrewFeed:
        assert self.server is not None
        f = CrewFeed(self.server.port, self.server.jwt, crew_id, since_seq=since_seq)
        self.feeds.append(f)
        return f

    # -- HTTP --------------------------------------------------------------------------------
    def human(self) -> httpx.Client:
        assert self.server is not None
        return httpx.Client(
            base_url=self.server.url + "/api/v1", headers={"Authorization": f"Bearer {self.server.jwt}"}, timeout=30
        )

    def api(self) -> httpx.Client:
        assert self.server is not None
        return httpx.Client(base_url=self.server.url + "/api/v1", headers={"X-API-Key": self.server.key}, timeout=30)

    def events(self, crew_id: str, since: int = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.human() as h:
            while True:
                r = h.get(f"/crews/{crew_id}/events", params={"since_seq": since, "limit": 200})
                r.raise_for_status()
                batch = r.json()["events"]
                out.extend(batch)
                if len(batch) < 200:
                    return out
                since = int(batch[-1]["seq"])

    def snapshot(self, crew_id: str) -> dict[str, Any]:
        with self.human() as h:
            r = h.get(f"/crews/{crew_id}/snapshot")
            r.raise_for_status()
            return dict(r.json())

    def crewd_pid(self) -> int | None:
        status = read_json(self.layout.status_file) or {}
        pid = status.get("pid")
        return int(pid) if pid and pid_alive(int(pid)) else None

    # -- teardown ----------------------------------------------------------------------------
    def close(self) -> None:
        for fn in self.closers:
            try:
                fn()
            except Exception:
                pass
        for a in self.agents:
            a.close()
        for f in self.feeds:
            f.close()
        pid = self.crewd_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            wait_for(lambda: not pid_alive(pid), timeout=10)
        if self.server is not None:
            self.server.stop()
        if self.receiver is not None:
            self.receiver.close()
