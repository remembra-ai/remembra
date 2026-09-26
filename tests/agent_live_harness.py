"""Run real agent CLIs (Gemini CLI, Qwen Code, Kimi Code) against local stand-ins, with no credentials.

- :class:`ModelStandIn` is a local model API. It speaks the Gemini API
  (``:streamGenerateContent``, ``:generateContent``, ``:countTokens``; Gemini
  CLI reaches it through ``GOOGLE_GEMINI_BASE_URL``, its "gateway" auth) and
  OpenAI chat completions (Qwen Code and Kimi Code reach it through an
  ``openai`` provider). Every turn gets a fixed reply, and every request body
  is kept, so a test can see what reached the model. ``fail_with`` makes the
  next chat turns fail (a 402 quota error, for Qwen's StopFailure).
- :class:`RecordingProxy` sits between the relay hooks and the local Remembra
  test server and keeps each request (path, agent header, JSON body): a test
  counts the briefs fetched and the closes posted, and reads their bodies.
- :class:`Pty` drives an interactive CLI in a pseudo-terminal and answers the
  cursor-position query some TUIs wait on.
- :func:`find_agent` finds a runnable binary: ``REMEMBRA_<AGENT>_BIN`` or the
  name on PATH, with its version.

Everything runs under a temp HOME; the user's own agent configs are never read
or written. See tests/test_relay_gemini_live.py, tests/test_relay_qwen_live.py
and tests/test_relay_kimi_live.py.
"""

from __future__ import annotations

import errno
import fcntl
import http.server
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from tests.codex_live_harness import scrub_text

FIXTURE_HOME = "/home/dev"
FIXTURE_REPO = "/home/dev/work/widget"
REPLY = "Stand-in reply: nothing to change."
REPLY_SHOWN = "Stand-in reply"  # what a TUI shows of it on one line, whatever it does with the rest


# `--version` prints the version alone on a line (a stub that prints a notice around one does not count:
# the archived kimi-cli's `kimi` only prints a deprecation notice).
VERSION_LINE = r"(?m)^\s*v?(\d+\.\d+\.\d+\S*)\s*$"


def find_agent(env_var: str, name: str, version_re: str = VERSION_LINE) -> tuple[str, str] | None:
    """(path, version) of the agent binary in ``$env_var``, else ``name`` on PATH; None when neither runs.

    ``--version`` runs with a throwaway HOME: even that writes into the agent's own
    directory (Gemini CLI leaves ``~/.gemini/projects.json.*.tmp`` files).
    """
    for candidate in (os.environ.get(env_var), shutil.which(name)):
        if not candidate or not os.path.exists(candidate):
            continue
        with tempfile.TemporaryDirectory(prefix="remembra-agent-version-") as scratch:
            env = {"PATH": agent_path(candidate), "HOME": scratch, "TMPDIR": scratch, "NO_COLOR": "1"}
            try:
                out = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=60, env=env, cwd=scratch)
            except (OSError, subprocess.TimeoutExpired):
                continue
        match = re.search(version_re, out.stdout)
        if out.returncode == 0 and match:
            return candidate, match.group(1)
    return None


def agent_path(binary: str) -> str:
    """A minimal PATH for an agent: the binary's own directory, then ``node``'s, then the system's."""
    dirs = [str(Path(binary).parent)]
    node = shutil.which("node")
    if node:
        dirs.append(str(Path(node).parent))
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


# ---------------------------------------------------------------------------
# Model stand-in
# ---------------------------------------------------------------------------


def _json_for_schema(schema: Any) -> Any:
    """A value that satisfies a (Gemini) JSON response schema: the first enum value, else a plain one per type."""
    if not isinstance(schema, dict):
        return "ok"
    if schema.get("enum"):
        return schema["enum"][0]
    kind = str(schema.get("type") or "").lower()
    if kind == "object" or "properties" in schema:
        return {key: _json_for_schema(value) for key, value in (schema.get("properties") or {}).items()}
    if kind == "array":
        return []
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return False
    return "ok"


class ModelStandIn:
    """A local model API that answers every turn with ``reply`` and keeps every request body."""

    def __init__(self, reply: str = REPLY) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.fail_with: tuple[int, dict[str, Any]] | None = None
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # model lists
                self._send(200, json.dumps({"object": "list", "data": [{"id": "stand-in", "object": "model"}]}).encode())

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                with owner.lock:
                    owner.requests.append({"path": self.path, "body": body})
                    failure = owner.fail_with
                if failure and "chat/completions" in self.path:
                    self._send(failure[0], json.dumps(failure[1]).encode())
                    return
                status, payload, content_type = owner.answer(self.path, body)
                self._send(status, payload, content_type)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> ModelStandIn:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, path: str, body: dict[str, Any]) -> tuple[int, bytes, str]:
        if ":countTokens" in path:
            return 200, b'{"totalTokens": 10}', "application/json"
        if ":generateContent" in path or ":streamGenerateContent" in path:
            config = body.get("generationConfig") or {}
            schema = config.get("responseJsonSchema") or config.get("responseSchema")
            text = json.dumps(_json_for_schema(schema)) if config.get("responseMimeType") == "application/json" else self.reply
            response = {
                "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
                "modelVersion": "stand-in",
            }
            if ":streamGenerateContent" in path:
                return 200, f"data: {json.dumps(response)}\r\n\r\n".encode(), "text/event-stream"
            return 200, json.dumps(response).encode(), "application/json"
        base = {"id": f"chatcmpl-{len(self.requests)}", "created": int(time.time()), "model": body.get("model") or "stand-in"}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if body.get("stream"):
            chunk = {**base, "object": "chat.completion.chunk"}
            chunks = [
                {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": self.reply}}]},
                {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
            ]
            stream = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return 200, stream.encode(), "text/event-stream"
        message = {"role": "assistant", "content": self.reply}
        completion = {**base, "object": "chat.completion", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
        return 200, json.dumps({**completion, "usage": usage}).encode(), "application/json"

    def turns(self) -> list[dict[str, Any]]:
        """The chat turns (not token counts, classifiers or other side requests), oldest first."""
        with self.lock:
            requests = list(self.requests)
        out = []
        for request in requests:
            path, body = request["path"], request["body"]
            if "chat/completions" in path or ":streamGenerateContent" in path:
                out.append(body)
        return out

    def wait_for_turns(self, n: int, timeout: float = 60) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while len(self.turns()) < n:
            if time.monotonic() > deadline:
                raise TimeoutError(f"the model got {len(self.turns())} turn(s), not {n}")
            time.sleep(0.1)
        return self.turns()


def request_text(body: dict[str, Any]) -> str:
    """Everything a turn sent to the model, as one string (Gemini ``contents`` or OpenAI ``messages``)."""
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body.get("contents") or body.get("messages") or [])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Remembra server proxy
# ---------------------------------------------------------------------------


class RecordingProxy:
    """Forwards to the local Remembra test server and keeps every request the hooks made."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _forward(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                agent = self.headers.get("X-Remembra-Agent-Id")
                with owner.lock:
                    owner.requests.append({"method": self.command, "path": self.path, "agent": agent, "body": body})
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
                res = httpx.request(self.command, owner.upstream + self.path, content=raw, headers=headers, timeout=30)
                self.send_response(res.status_code)
                self.send_header("content-type", res.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(res.content)))
                self.end_headers()
                self.wfile.write(res.content)

            do_GET = do_POST = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> RecordingProxy:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def briefs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "GET" and r["path"].startswith("/api/v1/session/brief")]

    def closes(self) -> list[dict[str, Any]]:
        """The ``/session/close`` bodies posted, oldest first."""
        with self.lock:
            return [r["body"] for r in self.requests if r["method"] == "POST" and r["path"] == "/api/v1/session/close"]

    def wait_for_closes(self, n: int, timeout: float = 45, settle: float = 3.0) -> list[dict[str, Any]]:
        """At least ``n`` closes, then ``settle`` seconds more (a repeat would arrive by then)."""
        deadline = time.monotonic() + timeout
        while len(self.closes()) < n and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(settle)
        return self.closes()


# ---------------------------------------------------------------------------
# Pseudo-terminal driver
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78DEHM]")


def plain(text: str) -> str:
    """Terminal output without escape sequences."""
    return _ANSI.sub("", text)


class Pty:
    """An interactive CLI in a pseudo-terminal (120x40), read by a background thread.

    Answers the cursor-position query (``ESC[6n``) a TUI may block on. The raw
    output is kept in :attr:`output`; :meth:`wait_for` matches the output with
    escape sequences removed.
    """

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: become the CLI
            try:
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.raw = b""
        self.lock = threading.Lock()
        self.reaping = threading.Lock()  # one waitpid at a time: the reader thread polls it too
        self.status: int | None = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                if self.exited():
                    return
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    return
                raise
            if not data:
                return
            with self.lock:
                self.raw += data
            if b"\x1b[6n" in data:
                self.write("\x1b[1;1R")

    @property
    def output(self) -> str:
        with self.lock:
            return plain(self.raw.decode("utf-8", "replace"))

    def write(self, text: str) -> None:
        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def type(self, text: str, enter: bool = True, pause: float = 0.03) -> None:
        """Type ``text`` one character at a time (TUIs treat a burst as a paste), then Enter."""
        for ch in text:
            self.write(ch)
            time.sleep(pause)
        if enter:
            time.sleep(0.3)
            self.write("\r")

    def wait_for(self, pattern: str | re.Pattern[str], timeout: float = 60, since: int = 0) -> re.Match[str]:
        """The first match of ``pattern`` in the output after character ``since``."""
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while True:
            match = regex.search(self.output, since)
            if match:
                return match
            if time.monotonic() > deadline or self.exited():
                tail = self.output[-2000:]
                raise TimeoutError(f"no {regex.pattern!r} in the terminal after {timeout}s; last output:\n{tail}")
            time.sleep(0.1)

    def wait_until(self, check: Callable[[], bool], timeout: float = 60, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{what} did not happen within {timeout}s; last output:\n{self.output[-2000:]}")
            time.sleep(0.1)

    def exited(self) -> bool:
        with self.reaping:
            if self.status is not None:
                return True
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.status = 0
                return True
            if pid == 0:
                return False
            self.status = status
            return True

    def wait_exit(self, timeout: float = 30) -> int:
        deadline = time.monotonic() + timeout
        while not self.exited():
            if time.monotonic() > deadline:
                raise TimeoutError(f"the CLI did not exit within {timeout}s; last output:\n{self.output[-2000:]}")
            time.sleep(0.1)
        assert self.status is not None
        return self.status

    def signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        if not self.exited():
            self.signal(signal.SIGKILL)
            with_timeout = time.monotonic() + 10
            while not self.exited() and time.monotonic() < with_timeout:
                time.sleep(0.05)
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Hook capture and fixtures
# ---------------------------------------------------------------------------


def tee_hook(path: Path, payloads: Path) -> Path:
    """The relay command the hooks run: saves each payload (and the agent's variables) to ``payloads``, then relays.

    ``connect --relay-command`` writes this path into the agent's config, so the
    agent runs exactly the hooks ``connect`` writes, with the payloads captured.
    """
    path.write_text(
        "#!/bin/sh\n"
        f'f=$(mktemp "{payloads}/payload.XXXXXX")\n'
        f'env | grep -E "^(GEMINI|QWEN|KIMI)_[A-Z_]*=" | sort > "$f.env"\n'
        f'tee "$f" | exec "{sys.executable}" -m remembra.relay.cli "$@"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def captured(payloads: Path) -> list[dict[str, Any]]:
    """The hook payloads saved by :func:`tee_hook`, in the order the hooks ran (empty stdin included as {})."""
    out = []
    for path in sorted(payloads.glob("payload.*"), key=lambda p: p.stat().st_mtime_ns):
        if path.suffix == ".env":
            continue
        text = path.read_text()
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {"_unparsed": text}
        env_file = path.with_name(path.name + ".env")
        env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line) if env_file.exists() else {}
        out.append({"payload": payload, "env": env, "file": path.name})
    return out


def by_event(payloads: Path) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for item in captured(payloads):
        events.setdefault(str(item["payload"].get("hook_event_name") or ""), []).append(item["payload"])
    return events


def record_fixture(directory: Path, name: str, payload: dict[str, Any], replacements: dict[str, str]) -> None:
    """Write one recorded payload, temp paths replaced with stable ``/home/dev/...`` ones."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(scrub_text(json.dumps(payload, indent=2, ensure_ascii=False), replacements) + "\n")


def _slug(path: str) -> str:
    """A path as Qwen Code names its per-project directory (``~/.qwen/projects/<slug>``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def replacements(tmp: Path, home: Path, repo: Path) -> dict[str, str]:
    """Temp paths (as given, resolved, and as a project-directory slug) -> the stable ``/home/dev/...`` ones."""
    real = {str(repo): FIXTURE_REPO, str(home): FIXTURE_HOME, str(tmp): "/home/dev/tmp"}
    real.update({os.path.realpath(k): v for k, v in list(real.items())})
    real.update({_slug(k): _slug(v) for k, v in list(real.items())})
    return real
