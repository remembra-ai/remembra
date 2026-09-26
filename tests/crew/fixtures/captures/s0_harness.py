#!/usr/bin/env python3
"""Spike S0 harness: live hook proofs against the installed Claude Code CLI.

This is the reproducible rig behind ``docs/crew/S0-results.md``. It drives the
real ``claude`` binary non-interactively (``claude -p``) inside throwaway git
repos that carry **project-level** ``.claude/settings.json`` hooks only. It never
reads or writes the user's ``~/.claude`` configuration:

* ``--setting-sources project`` keeps user/local settings (and their hooks) out.
* ``--strict-mcp-config`` with no ``--mcp-config`` keeps every MCP server out.
* ``mock`` mode additionally sets ``CLAUDE_CONFIG_DIR`` to a temp directory,
  ``ANTHROPIC_API_KEY`` to a dummy value and ``ANTHROPIC_BASE_URL`` to a local
  mock of the Messages API. No real credential leaves the machine, no credits
  are spent, and the mock logs the exact request bodies Claude Code sends, which
  is the strongest available proof of what "reaches the model".
* ``real`` mode uses the user's normal login against the real API. It is only
  used for a handful of short runs (deny + context, invalid model) and always
  passes ``--no-session-persistence``.

Every hook is ``s0_hook.py``, which records the raw stdin payload in firing order.

CLI::

    python s0_harness.py --workdir /tmp/s0 --out tests/crew/fixtures/captures/claude-code-2.1.168
    python s0_harness.py --workdir /tmp/s0 --only deny,context --no-write
    python s0_harness.py --workdir /tmp/s0 --mode interactive
    python s0_harness.py --workdir /tmp/s0 --mode real      # uses your login; a few short calls

The pytest module ``test_s0_captures.py`` re-runs the mock scenarios when
``REMEMBRA_S0_LIVE=1`` and a ``claude`` binary is available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HOOK = HERE / "s0_hook.py"
DUMMY_KEY = "sk-ant-api03-s0-dummy-not-a-real-key-000000000000000000000000000000000000"

# ---------------------------------------------------------------------------
# Mock Messages API
# ---------------------------------------------------------------------------

ERROR_RESPONSES: dict[str, tuple[int, dict[str, str], dict[str, Any]]] = {
    # Real API wording for an exhausted prepaid balance (Claude Code matches
    # "credit balance is too low" case-insensitively and maps it to billing_error).
    "billing": (
        400,
        {},
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits.",
            },
        },
    ),
    # 429 with the unified rate-limit headers a subscription usage limit carries.
    "ratelimit": (
        429,
        {
            "anthropic-ratelimit-unified-status": "rejected",
            "anthropic-ratelimit-unified-reset": str(int(time.time()) + 3600),
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
            "retry-after": "3600",
        },
        {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "This request would exceed your rate limit."},
        },
    ),
    "overloaded": (529, {}, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    "auth": (401, {}, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
    "model": (
        404,
        {},
        {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-s0-does-not-exist"}},
    ),
}


@dataclass
class Plan:
    """What the mock model does for one scenario."""

    tool: dict[str, Any] | None = None  # {"name": "Write", "input": {...}} issued once on the main loop
    error: str | None = None  # key of ERROR_RESPONSES, returned for every /v1/messages call
    final_text: str = "S0 ack"
    final_delay_s: float = 0.0  # delay before answering a request that carries a tool_result


class MockAnthropic:
    """Minimal streaming Messages API. Records every request body in order."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.plan = Plan()
        self.tool_issued = False
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                return

            def _send_json(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("request-id", "req_s0mock")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                self._send_json(200, {"data": [], "has_more": False})

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                path = self.path.split("?")[0]
                with mock._lock:
                    mock.requests.append({"path": path, "t": time.time(), "body": body})
                if path.endswith("/count_tokens"):
                    self._send_json(200, {"input_tokens": 12})
                    return
                if not path.endswith("/v1/messages"):
                    self._send_json(200, {})
                    return
                if mock.plan.error:
                    status, headers, err = ERROR_RESPONSES[mock.plan.error]
                    self._send_json(status, err, headers)
                    return
                content = mock._decide(body)
                if (
                    content[0]["type"] == "text"
                    and mock.plan.final_delay_s
                    and "tool_result" in json.dumps(body.get("messages", [])[-1:])
                ):
                    time.sleep(mock.plan.final_delay_s)
                stop = "tool_use" if content[0]["type"] == "tool_use" else "end_turn"
                if body.get("stream"):
                    self._stream(body, content, stop)
                else:
                    self._send_json(200, mock._message(body, content, stop))

            def _stream(self, body: dict[str, Any], content: list[dict[str, Any]], stop: str) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("request-id", "req_s0mock")
                self.end_headers()

                def ev(name: str, data: dict[str, Any]) -> None:
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                msg = mock._message(body, [], None)
                ev("message_start", {"type": "message_start", "message": msg})
                for i, block in enumerate(content):
                    if block["type"] == "text":
                        ev(
                            "content_block_start",
                            {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                        )
                        ev(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}},
                        )
                    else:
                        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                        ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": start})
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
                    ev("content_block_stop", {"type": "content_block_stop", "index": i})
                ev(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 7},
                    },
                )
                ev("message_stop", {"type": "message_stop"})
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset(self, plan: Plan) -> None:
        with self._lock:
            self.requests = []
            self.plan = plan
            self.tool_issued = False

    def _message(self, body: dict[str, Any], content: list[dict[str, Any]], stop: str | None) -> dict[str, Any]:
        return {
            "id": f"msg_s0_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-s0-mock"),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }

    def _decide(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        last_content = last.get("content") if isinstance(last, dict) else None
        has_tool_result = isinstance(last_content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in last_content
        )
        plan = self.plan
        with self._lock:
            if plan.tool and plan.tool["name"] in tools and not has_tool_result and not self.tool_issued:
                self.tool_issued = True
                return [
                    {
                        "type": "tool_use",
                        "id": f"toolu_s0_{uuid.uuid4().hex[:12]}",
                        "name": plan.tool["name"],
                        "input": plan.tool["input"],
                    }
                ]
        return [{"type": "text", "text": plan.final_text}]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


def hook_entry(capdir: Path, event: str, action: str, *args: str, matcher: str | None = None, **opts: Any) -> dict[str, Any]:
    cmd = " ".join(shlex.quote(p) for p in [sys.executable, str(HOOK), str(capdir), event, action, *args])
    hook: dict[str, Any] = {"type": "command", "command": cmd + " # remembra-crew-s0", "timeout": opts.pop("timeout", 20)}
    hook.update(opts)
    entry: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


@dataclass
class Scenario:
    name: str
    prompt: str
    plan: Plan
    # builder(capdir, repo) -> {"Event": [entry, ...]}
    hooks: Callable[[Path, Path], dict[str, list[dict[str, Any]]]]
    model: str = "claude-sonnet-4-5"
    post_wait: float = 0.0
    tool_builder: Callable[[Path], dict[str, Any]] | None = None
    base_url_override: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass
class RunResult:
    scenario: str
    mode: str
    returncode: int
    duration_s: float
    repo: Path
    capdir: Path
    stdout_lines: list[dict[str, Any]]
    stderr: str
    requests: list[dict[str, Any]]
    captures: list[dict[str, Any]] = field(default_factory=list)
    markers_at_exit: list[str] = field(default_factory=list)
    markers_after_wait: list[str] = field(default_factory=list)

    def events(self) -> list[str]:
        return [c["event"] for c in self.captures]

    def capture(self, event: str) -> dict[str, Any] | None:
        for c in self.captures:
            if c["event"] == event:
                return c
        return None

    def requests_text(self) -> list[str]:
        return [json.dumps(r["body"]) for r in self.requests if r["path"].endswith("/v1/messages")]


def _git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README.md").write_text("# s0 scratch repo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=s0", "-c", "user.email=s0@example.invalid", "commit", "-qm", "init"],
        check=True,
        env=env,
    )


def find_claude() -> str | None:
    return shutil.which("claude") or (
        "/Users/dolphy/.npm-global/bin/claude" if Path("/Users/dolphy/.npm-global/bin/claude").exists() else None
    )


def run(
    sc: Scenario,
    workdir: Path,
    *,
    mode: str = "mock",
    mock: MockAnthropic | None = None,
    claude_bin: str | None = None,
    timeout: float = 180.0,
) -> RunResult:
    claude_bin = claude_bin or find_claude()
    if not claude_bin:
        raise RuntimeError("claude CLI not found")
    root = workdir / f"{mode}-{sc.name}"
    if root.exists():
        shutil.rmtree(root)
    repo, capdir = root / "repo", root / "raw"
    capdir.mkdir(parents=True)
    _git_repo(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": sc.hooks(capdir, repo)}, indent=2))

    plan = Plan(
        tool=sc.tool_builder(repo) if sc.tool_builder else sc.plan.tool,
        error=sc.plan.error,
        final_text=sc.plan.final_text,
        final_delay_s=sc.plan.final_delay_s,
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    env.update({"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_MAX_RETRIES": "0"})
    if mode == "mock":
        if mock is None:
            raise ValueError("mock mode needs a MockAnthropic")
        mock.reset(plan)
        cfg = root / "config"
        cfg.mkdir()
        env.update(
            {
                "CLAUDE_CONFIG_DIR": str(cfg),
                "ANTHROPIC_API_KEY": DUMMY_KEY,
                "ANTHROPIC_BASE_URL": sc.base_url_override or mock.base_url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
            }
        )
    args = [
        claude_bin,
        "-p",
        sc.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--model",
        sc.model,
        "--max-turns",
        "4",
    ]
    args += list(sc.extra_args)
    if mode == "real":
        args.append("--no-session-persistence")
    t0 = time.time()
    proc = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    duration = time.time() - t0
    markers_at_exit = sorted(p.name for p in capdir.glob("*.done"))
    if sc.post_wait:
        time.sleep(sc.post_wait)
    markers_after = sorted(p.name for p in capdir.glob("*.done"))
    lines = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                lines.append(json.loads(line))
            except ValueError:
                lines.append({"_text": line})
    captures = [json.loads(p.read_text()) | {"_file": p.name} for p in sorted(capdir.glob("*.json"))]
    return RunResult(
        scenario=sc.name,
        mode=mode,
        returncode=proc.returncode,
        duration_s=round(duration, 2),
        repo=repo,
        capdir=capdir,
        stdout_lines=lines,
        stderr=proc.stderr,
        requests=list(mock.requests) if (mode == "mock" and mock) else [],
        captures=captures,
        markers_at_exit=markers_at_exit,
        markers_after_wait=markers_after,
    )


def run_interactive(
    sc: Scenario,
    workdir: Path,
    mock: MockAnthropic,
    *,
    claude_bin: str | None = None,
    settle_s: float = 12.0,
) -> RunResult:
    """Run one scenario in the interactive TUI on a pseudo-terminal (mock API only).

    The prompt is passed as the initial message. The TUI is left idle for
    ``settle_s`` seconds (so an ``asyncRewake`` hook can wake it), then ``/exit``
    is typed and the process is given 10 s to end before it is terminated.
    """
    import pty
    import select
    import signal

    claude_bin = claude_bin or find_claude()
    if not claude_bin:
        raise RuntimeError("claude CLI not found")
    root = workdir / f"interactive-{sc.name}"
    if root.exists():
        shutil.rmtree(root)
    repo, capdir, cfg = root / "repo", root / "raw", root / "config"
    capdir.mkdir(parents=True)
    cfg.mkdir()
    _git_repo(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": sc.hooks(capdir, repo)}, indent=2))
    # Pre-accept first-run dialogs in the throwaway config dir only.
    trusted = {str(repo): {"hasTrustDialogAccepted": True}, str(repo.resolve()): {"hasTrustDialogAccepted": True}}
    (cfg / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.1.168",
                "theme": "dark",
                "customApiKeyResponses": {"approved": [DUMMY_KEY[-20:]], "rejected": []},
                "projects": trusted,
            }
        )
    )
    mock.reset(
        Plan(
            tool=sc.tool_builder(repo) if sc.tool_builder else sc.plan.tool,
            error=sc.plan.error,
            final_text=sc.plan.final_text,
            final_delay_s=sc.plan.final_delay_s,
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    env.update(
        {
            "CLAUDE_CONFIG_DIR": str(cfg),
            "ANTHROPIC_API_KEY": DUMMY_KEY,
            "ANTHROPIC_BASE_URL": mock.base_url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "TERM": "xterm-256color",
            "COLUMNS": "120",
            "LINES": "40",
        }
    )
    args = [
        claude_bin,
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--model",
        sc.model,
    ]
    args += list(sc.extra_args) + [sc.prompt]
    master, slave = pty.openpty()
    t0 = time.time()
    proc = subprocess.Popen(
        args, cwd=repo, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True
    )
    os.close(slave)
    screen = bytearray()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                screen.extend(chunk)

    pump(settle_s)
    markers_idle = sorted(p.name for p in capdir.glob("*.done"))
    for keys in (b"/exit", b"\r"):
        os.write(master, keys)
        pump(0.5)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    pump(0.5)
    os.close(master)
    duration = time.time() - t0
    if sc.post_wait:
        time.sleep(sc.post_wait)
    captures = [json.loads(p.read_text()) | {"_file": p.name} for p in sorted(capdir.glob("*.json"))]
    text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", bytes(screen)).decode("utf-8", "replace")
    return RunResult(
        scenario=sc.name,
        mode="interactive",
        returncode=proc.returncode,
        duration_s=round(duration, 2),
        repo=repo,
        capdir=capdir,
        stdout_lines=[{"_screen_tail": text[-4000:]}],
        stderr="",
        requests=list(mock.requests),
        captures=captures,
        markers_at_exit=markers_idle,
        markers_after_wait=sorted(p.name for p in capdir.glob("*.done")),
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

NONCE = uuid.uuid4().hex[:8]
DENY_REASON = f"S0-DENY-{NONCE}: Crew: src/pos is in zone pos held by codex-1. Do not modify it."
SS_CTX = f"S0-SESSIONSTART-CTX-{NONCE}"
UPS_CTX = f"S0-PROMPT-CTX-{NONCE}"
PTU_CTX = f"S0-PRETOOL-CTX-{NONCE}"
ASYNC_CTX = f"S0-ASYNC-POST-CTX-{NONCE}"
REWAKE_MSG = f"S0-REWAKE-{NONCE}: claim on zone pos granted to you"
ENV_VALUE = f"s0env{NONCE}"


def _write_tool(repo: Path) -> dict[str, Any]:
    return {"name": "Write", "input": {"file_path": str(repo / "pos" / "cart.txt"), "content": "s0 write\n"}}


def _record_all(capdir: Path, *events: str) -> dict[str, list[dict[str, Any]]]:
    return {e: [hook_entry(capdir, e, "record")] for e in events}


def scenarios() -> dict[str, Scenario]:
    def deny_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")
        h["PreToolUse"] = [
            hook_entry(capdir, "PreToolUse", "deny", DENY_REASON, matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*")
        ]
        return h

    def context_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "PostToolUse", "Stop", "SessionEnd")
        h["SessionStart"] = [hook_entry(capdir, "SessionStart", "ctx", SS_CTX)]
        h["UserPromptSubmit"] = [hook_entry(capdir, "UserPromptSubmit", "ctx", UPS_CTX)]
        h["PreToolUse"] = [
            hook_entry(capdir, "PreToolUse", "ctx", PTU_CTX, matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*")
        ]
        return h

    def envfile_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "PreToolUse", "PostToolUse", "SessionEnd")
        h["SessionStart"] = [hook_entry(capdir, "SessionStart", "envfile", "S0_CREW_ENV", ENV_VALUE)]
        return h

    def async_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["PostToolUse"] = [
            hook_entry(capdir, "PostToolUse", "sleepctx", "4", ASYNC_CTX, matcher="Edit|Write", **{"async": True, "timeout": 30})
        ]
        return h

    def async_fast_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["PostToolUse"] = [
            hook_entry(
                capdir, "PostToolUse", "sleepctx", "0.2", ASYNC_CTX, matcher="Edit|Write", **{"async": True, "timeout": 30}
            )
        ]
        return h

    def rewake_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["PostToolUse"] = [
            hook_entry(capdir, "PostToolUse", "rewake", "3", REWAKE_MSG, matcher="Edit|Write", asyncRewake=True, timeout=30)
        ]
        return h

    def rewake_quiet_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        # asyncRewake hook that finishes with exit 0: shows whether -p still waits for it.
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["PostToolUse"] = [
            hook_entry(capdir, "PostToolUse", "sleepctx", "3", ASYNC_CTX, matcher="Edit|Write", asyncRewake=True, timeout=30)
        ]
        return h

    def rewake_idle_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        # asyncRewake on Stop: the agent has finished its turn (idle). Does the hook wake it?
        h = _record_all(capdir, "UserPromptSubmit", "SessionEnd")
        h["Stop"] = [hook_entry(capdir, "Stop", "rewake_once", "3", REWAKE_MSG, asyncRewake=True, timeout=30)]
        return h

    def failure_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        return _record_all(capdir, "SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")

    def slow_failure_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        # A StopFailure hook that needs 3 s (like `remembra-crew stall`): does -p wait for it?
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["StopFailure"] = [hook_entry(capdir, "StopFailure", "sleepctx", "3", "S0 stall done", timeout=20)]
        return h

    write_prompt = "S0 spike: use the Write tool to create pos/cart.txt containing 's0 write'. Then say what happened."
    out: dict[str, Scenario] = {
        "deny": Scenario("deny", write_prompt, Plan(), deny_hooks, tool_builder=_write_tool),
        "context": Scenario("context", write_prompt, Plan(), context_hooks, tool_builder=_write_tool),
        "envfile": Scenario(
            "envfile",
            "S0 spike: run `printenv S0_CREW_ENV` with Bash.",
            Plan(tool={"name": "Bash", "input": {"command": "printenv S0_CREW_ENV", "description": "Print S0 env"}}),
            envfile_hooks,
            extra_args=("--allowedTools", "Bash(printenv:*)"),
        ),
        "async_post": Scenario("async_post", write_prompt, Plan(), async_hooks, tool_builder=_write_tool, post_wait=6),
        "async_post_fast": Scenario(
            "async_post_fast", write_prompt, Plan(), async_fast_hooks, tool_builder=_write_tool, post_wait=4
        ),
        "async_post_settled": Scenario(
            "async_post_settled", write_prompt, Plan(final_delay_s=2.5), async_fast_hooks, tool_builder=_write_tool, post_wait=2
        ),
        "async_rewake": Scenario("async_rewake", write_prompt, Plan(), rewake_hooks, tool_builder=_write_tool, post_wait=6),
        "async_rewake_quiet": Scenario(
            "async_rewake_quiet", write_prompt, Plan(), rewake_quiet_hooks, tool_builder=_write_tool, post_wait=6
        ),
        "async_rewake_idle": Scenario("async_rewake_idle", "S0 spike: say hi.", Plan(), rewake_idle_hooks, post_wait=6),
        "stopfailure_slow_hook": Scenario(
            "stopfailure_slow_hook", "S0 spike: say hi.", Plan(error="billing"), slow_failure_hooks, post_wait=6
        ),
    }
    for err in ("billing", "ratelimit", "overloaded", "auth", "model"):
        out[f"stopfailure_{err}"] = Scenario(f"stopfailure_{err}", "S0 spike: say hi.", Plan(error=err), failure_hooks)
    out["stopfailure_network"] = Scenario(
        "stopfailure_network", "S0 spike: say hi.", Plan(), failure_hooks, base_url_override=f"http://127.0.0.1:{_closed_port()}"
    )
    return out


def real_scenarios() -> dict[str, Scenario]:
    """Scenarios run against the real API with the user's login (few, short)."""

    def deny_ctx_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "PostToolUse", "Stop", "SessionEnd", "StopFailure")
        h["SessionStart"] = [hook_entry(capdir, "SessionStart", "ctx", SS_CTX)]
        h["UserPromptSubmit"] = [hook_entry(capdir, "UserPromptSubmit", "ctx", UPS_CTX)]
        h["PreToolUse"] = [
            hook_entry(capdir, "PreToolUse", "deny", DENY_REASON, matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*")
        ]
        return h

    def ctx_allow_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "PostToolUse", "Stop", "SessionEnd", "StopFailure")
        h["PreToolUse"] = [
            hook_entry(capdir, "PreToolUse", "ctx", PTU_CTX, matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*")
        ]
        return h

    def failure_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        return _record_all(capdir, "SessionStart", "Stop", "StopFailure", "SessionEnd")

    report = (
        " Afterwards reply with: (1) whether the file was written, (2) the exact text of any hook denial reason you received,"
        " (3) every line in your context that starts with 'S0-', verbatim. Do not retry the write and do not use any other tool."
    )
    return {
        "real_deny_ctx": Scenario(
            "real_deny_ctx",
            "Use the Write tool once to create the file pos/cart.txt with the content 's0'." + report,
            Plan(),
            deny_ctx_hooks,
            model="haiku",
        ),
        "real_pretool_ctx": Scenario(
            "real_pretool_ctx",
            "Use the Write tool once to create the file notes.txt with the content 's0'." + report,
            Plan(),
            ctx_allow_hooks,
            model="haiku",
        ),
        "real_stopfailure_model": Scenario(
            "real_stopfailure_model", "Say hi.", Plan(), failure_hooks, model="claude-s0-does-not-exist-20990101"
        ),
    }


def interactive_scenarios() -> dict[str, Scenario]:
    """Scenarios run in the interactive TUI on a pty against the mock API."""

    def hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd")
        h["PreToolUse"] = [
            hook_entry(capdir, "PreToolUse", "deny", DENY_REASON, matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*")
        ]
        h["Stop"] = [hook_entry(capdir, "Stop", "rewake_once", "3", REWAKE_MSG, asyncRewake=True, timeout=30)]
        return h

    def async_hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
        h = _record_all(capdir, "Stop", "SessionEnd")
        h["PostToolUse"] = [
            hook_entry(capdir, "PostToolUse", "sleepctx", "4", ASYNC_CTX, matcher="Edit|Write", **{"async": True, "timeout": 30})
        ]
        return h

    return {
        "interactive_deny_rewake": Scenario(
            "interactive_deny_rewake",
            "S0 spike: use the Write tool to create pos/cart.txt.",
            Plan(),
            hooks,
            tool_builder=_write_tool,
        ),
        "interactive_async_post": Scenario(
            "interactive_async_post",
            "S0 spike: use the Write tool to create pos/cart.txt.",
            Plan(),
            async_hooks,
            tool_builder=_write_tool,
        ),
    }


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _tool_results(res: RunResult) -> list[dict[str, Any]]:
    """tool_result blocks the model received (from the request log, else from stream-json user turns)."""
    out: list[dict[str, Any]] = []
    if res.requests:
        for r in res.requests:
            for m in (r["body"].get("messages") or [])[-1:]:
                content = m.get("content")
                if isinstance(content, list):
                    out += [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        return out
    for line in res.stdout_lines:
        if line.get("type") == "user":
            for b in (line.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(b)
    return out


def _result_text(res: RunResult) -> str:
    for line in reversed(res.stdout_lines):
        if line.get("type") == "result":
            return str(line.get("result") or "")
    return ""


def verdict(res: RunResult) -> dict[str, Any]:
    name = res.scenario
    v: dict[str, Any] = {"events": res.events(), "returncode": res.returncode, "duration_s": res.duration_s}
    v["sessionend_fired"] = "SessionEnd" in res.events()
    msg_reqs = [r for r in res.requests if r["path"].endswith("/v1/messages")]
    if msg_reqs:
        v["request_gaps_s"] = [round(b["t"] - a["t"], 2) for a, b in zip(msg_reqs, msg_reqs[1:], strict=False)]
    reqs = res.requests_text()
    target = res.repo / "pos" / "cart.txt"
    if name in ("deny", "real_deny_ctx"):
        trs = _tool_results(res)
        v["file_written"] = target.exists()
        v["deny_reason_in_tool_result"] = any(DENY_REASON in json.dumps(t) for t in trs)
        v["tool_result_is_error"] = any(t.get("is_error") for t in trs if DENY_REASON in json.dumps(t))
        v["posttooluse_fired"] = "PostToolUse" in res.events()
        if name == "real_deny_ctx":
            text = _result_text(res)
            v["model_quoted_deny_nonce"] = f"S0-DENY-{NONCE}" in text
            v["model_quoted_sessionstart_ctx"] = SS_CTX in text
            v["model_quoted_prompt_ctx"] = UPS_CTX in text
        v["ok"] = (not v["file_written"]) and v["deny_reason_in_tool_result"]
    elif name == "context":
        first = next((r for r in reqs), "")
        v["file_written"] = target.exists()
        v["sessionstart_ctx_in_first_request"] = SS_CTX in first
        v["prompt_ctx_in_first_request"] = UPS_CTX in first
        v["pretool_ctx_in_later_request"] = any(PTU_CTX in r for r in reqs[1:])
        v["ok"] = all(
            v[k]
            for k in (
                "file_written",
                "sessionstart_ctx_in_first_request",
                "prompt_ctx_in_first_request",
                "pretool_ctx_in_later_request",
            )
        )
    elif name == "real_pretool_ctx":
        text = _result_text(res)
        v["file_written"] = (res.repo / "notes.txt").exists()
        v["model_quoted_pretool_ctx"] = PTU_CTX in text
        v["pretool_ctx_in_stream"] = any(PTU_CTX in json.dumps(line) for line in res.stdout_lines)
        v["ok"] = v["file_written"] and v["model_quoted_pretool_ctx"]
    elif name == "envfile":
        trs = _tool_results(res)
        v["env_value_in_bash_output"] = any(ENV_VALUE in json.dumps(t) for t in trs)
        ss = res.capture("SessionStart")
        v["claude_env_file_set_for_sessionstart"] = bool(ss and ss["env"]["CLAUDE_ENV_FILE_set"])
        v["ok"] = v["env_value_in_bash_output"]
    elif name in ("async_post", "async_post_fast", "async_post_settled", "interactive_async_post"):
        v["file_written"] = target.exists()
        v["marker_at_exit"] = "PostToolUse.done" in res.markers_at_exit
        v["marker_after_wait"] = "PostToolUse.done" in res.markers_after_wait
        v["async_ctx_reached_model"] = any(ASYNC_CTX in r for r in reqs)
        if res.mode != "interactive":
            v["run_shorter_than_hook_sleep"] = res.duration_s < 4
        v["ok"] = v["file_written"]
    elif name == "async_rewake_quiet":
        v["file_written"] = target.exists()
        v["marker_at_exit"] = "PostToolUse.done" in res.markers_at_exit
        v["async_ctx_reached_model"] = any(ASYNC_CTX in r for r in reqs)
        v["messages_requests"] = len(reqs)
        v["ok"] = v["file_written"]
    elif name == "interactive_deny_rewake":
        v["file_written"] = target.exists()
        v["deny_reason_reached_model"] = any(DENY_REASON in r for r in reqs)
        v["rewake_msg_reached_model_while_idle"] = any(REWAKE_MSG in r for r in reqs)
        v["messages_requests"] = len(reqs)
        v["ok"] = (not v["file_written"]) and v["deny_reason_reached_model"] and v["rewake_msg_reached_model_while_idle"]
    elif name == "async_rewake_idle":
        v["marker_at_exit"] = "Stop.done" in res.markers_at_exit
        v["rewake_msg_reached_model"] = any(REWAKE_MSG in r for r in reqs)
        v["messages_requests"] = len(reqs)
        v["stop_count"] = res.events().count("Stop")
        v["ok"] = v["rewake_msg_reached_model"]
    elif name == "async_rewake":
        v["file_written"] = target.exists()
        v["marker_at_exit"] = "PostToolUse.done" in res.markers_at_exit
        v["rewake_msg_reached_model"] = any(REWAKE_MSG in r for r in reqs)
        v["messages_requests"] = len(reqs)
        v["ok"] = v["file_written"]
    elif name.startswith(("stopfailure_", "real_stopfailure_")):
        sf = res.capture("StopFailure")
        v["stopfailure_fired"] = sf is not None
        v["stop_fired"] = "Stop" in res.events()
        v["error"] = (sf or {}).get("payload", {}).get("error") if sf else None
        v["error_details"] = (sf or {}).get("payload", {}).get("error_details") if sf else None
        v["last_assistant_message"] = (sf or {}).get("payload", {}).get("last_assistant_message") if sf else None
        if name == "stopfailure_slow_hook":
            v["slow_hook_finished_before_exit"] = "StopFailure.done" in res.markers_at_exit
            v["slow_hook_finished_after_wait"] = "StopFailure.done" in res.markers_after_wait
        v["ok"] = v["stopfailure_fired"]
    return v


# ---------------------------------------------------------------------------
# Sanitising and writing fixtures
# ---------------------------------------------------------------------------


def sanitize(obj: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(obj, str):
        for old, new in replacements:
            obj = obj.replace(old, new)
        obj = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "<API_KEY>", obj)
        return obj
    if isinstance(obj, list):
        return [sanitize(x, replacements) for x in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v, replacements) for k, v in obj.items()}
    return obj


def _replacements(workdir: Path) -> list[tuple[str, str]]:
    reps = []
    for w in {str(workdir), str(workdir.resolve()), "/private" + str(workdir) if not str(workdir).startswith("/private") else ""}:
        if w:
            reps.append((w, "<S0_TMP>"))
    # Claude Code also embeds the cwd in transcript paths, encoded as [^A-Za-z0-9] -> "-".
    for w in {str(workdir), str(workdir.resolve())}:
        reps.append((re.sub(r"[^A-Za-z0-9]", "-", w), "-S0-TMP"))
    reps.append((sys.executable, "<PYTHON>"))
    reps.append((str(HOOK), "<S0_HOOK>"))
    reps.append((str(Path.home()), "<HOME>"))
    reps.sort(key=lambda p: -len(p[0]))
    return reps


def _elide(message: Any) -> Any:
    """Keep only the blocks that matter for S0 (tool results, hook text, nonces); elide the rest."""
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    kept = []
    for block in content or []:
        blob = json.dumps(block)
        if block.get("type") in ("tool_result", "tool_use") or re.search(r"S0-|hook (feedback|blocking)", blob):
            kept.append(block)
        else:
            kept.append({"type": block.get("type"), "_elided_chars": len(blob)})
    return {"role": message.get("role"), "content": kept}


def write_fixtures(res: RunResult, v: dict[str, Any], out: Path, workdir: Path) -> None:
    reps = _replacements(workdir)
    dest = out / res.mode / res.scenario
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for cap in res.captures:
        name = cap.pop("_file")
        (dest / name).write_text(json.dumps(sanitize(cap, reps), indent=2, sort_keys=True) + "\n")
    hook_events = [
        line for line in res.stdout_lines if line.get("type") == "system" and str(line.get("subtype", "")).startswith("hook")
    ]
    summary = {
        "claude_code_version": claude_version(),
        "scenario": res.scenario,
        "mode": res.mode,
        "nonce": NONCE,
        "verdict": v,
        "result_text": _result_text(res)[:1500],
        "tool_results_seen_by_model": _tool_results(res),
        "hook_events_in_stream": hook_events[:20],
        "stderr_tail": res.stderr[-1500:],
    }
    if res.requests:
        summary["messages_requests"] = [
            {
                "i": i,
                "model": r["body"].get("model"),
                "n_messages": len(r["body"].get("messages") or []),
                "last_message": _elide((r["body"].get("messages") or [None])[-1]),
                "system_has_nonce": NONCE in json.dumps(r["body"].get("system")),
            }
            for i, r in enumerate(x for x in res.requests if x["path"].endswith("/v1/messages"))
        ]
    (dest / "summary.json").write_text(json.dumps(sanitize(summary, reps), indent=2, sort_keys=True) + "\n")


def claude_version() -> str:
    exe = find_claude()
    if not exe:
        return "unknown"
    try:
        return subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=HERE / "claude-code-2.1.168")
    ap.add_argument("--only", default="", help="comma-separated scenario names")
    ap.add_argument("--mode", choices=("mock", "real", "interactive"), default="mock")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    a.workdir.mkdir(parents=True, exist_ok=True)
    table = {"mock": scenarios, "real": real_scenarios, "interactive": interactive_scenarios}[a.mode]()
    names = [n for n in a.only.split(",") if n] or list(table)
    mock = None if a.mode == "real" else MockAnthropic()
    try:
        for n in names:
            if a.mode == "interactive":
                assert mock is not None
                res = run_interactive(table[n], a.workdir, mock, settle_s=15)
            else:
                res = run(table[n], a.workdir, mode=a.mode, mock=mock)
            v = verdict(res)
            print(json.dumps({"scenario": n, **v}, default=str))
            if not a.no_write:
                write_fixtures(res, v, a.out, a.workdir)
    finally:
        if mock:
            mock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
