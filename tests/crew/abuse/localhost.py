"""One developer machine for the red-team runs: temp HOME, the vendored gate, a real crewd, real repos.

Everything lives under the test's tmp_path (``HOME`` is a temp directory: the real
``~/.claude``, ``~/.remembra``, LaunchAgents and global git hooks are never read or
written). ``connect`` installs crew mode through the real consent flow (answered at a
pseudo-terminal, no service). The gate runs exactly as Claude Code runs it (``python -I
<HOME>/.remembra/crew/bin/crew-gate.py <verb> --hook claude-code`` with the hook JSON on
stdin); ``remembra-crew`` runs as the SessionStart/StopFailure/SessionEnd hook does; crewd
is the real daemon the first hook spawns, talking to the red-team server over HTTP.

Each model-free agent is a real process (:class:`Agent`, ``agent_shim.py``) and every hook,
git and shell command of that agent runs as its child, so crewd's process-ancestry identity
and the git gates see exactly what they see under a real ``claude`` process. The checkouts
are one repository with a worktree per agent, as §13.3 sets them up.
"""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from remembra.relay.crew.gate import Layout, read_json, session_key, vendor_gate
from tests.crew.wp9_support import add_worktree, git, make_repo

T = TypeVar("T")

PY = sys.executable
ROOT = Path(__file__).resolve().parents[3]
SHIM = Path(__file__).with_name("agent_shim.py")

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
"""


def wait_for(fn: Callable[[], T], timeout: float = 15.0, interval: float = 0.2) -> T:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(res.stdout.strip()) and not res.stdout.strip().startswith("Z")


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str

    @property
    def denied(self) -> bool:
        return '"permissionDecision":"deny"' in self.stdout.replace(" ", "")

    @property
    def reason(self) -> str:
        if not self.stdout.strip():
            return ""
        out = json.loads(self.stdout)["hookSpecificOutput"]
        return str(out.get("permissionDecisionReason") or out.get("additionalContext") or "")


class Agent:
    """One model-free Claude Code session: a real parent process whose children are its hooks and commands."""

    def __init__(self, host: LocalHost, name: str, checkout: Path) -> None:
        self.host = host
        self.name = name
        self.client_session = f"sess-{name}"
        self.cwd = checkout
        self.proc = subprocess.Popen(
            [PY, "-u", str(SHIM)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(checkout)
        )
        self.pid = self.proc.pid

    def _run(self, req: dict[str, Any]) -> Result:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        req.setdefault("env", self.host.env())
        req.setdefault("cwd", str(self.cwd))
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"agent {self.name} shim exited"
        out = json.loads(line)
        return Result(int(out["rc"]), str(out["stdout"]), str(out["stderr"]))

    def run(self, argv: list[str], *, input: str = "", env: dict[str, str] | None = None) -> Result:
        return self._run({"argv": argv, "input": input, **({"env": env} if env else {})})

    def shell(self, command: str) -> Result:
        """Run a command as the agent's Bash tool would (the real effect on disk)."""
        return self._run({"shell": command})

    def git(self, *args: str) -> Result:
        return self.run(["git", *args])

    def cli(self, *args: str, input: str = "") -> Result:
        """``remembra-crew <args>`` run by this agent (inside its process tree, as its Bash tool would)."""
        return self.run([PY, "-m", "remembra.relay.crew.cli", *args], input=input)

    def stall(self, error: str = "billing_error", last: str = "Credit balance is too low") -> Result:
        """The StopFailure hook (``remembra-crew stall``) with Claude Code's payload."""
        payload = {
            "session_id": self.client_session,
            "cwd": str(self.cwd),
            "hook_event_name": "StopFailure",
            "error": error,
            "error_details": "credits",
            "last_assistant_message": last,
            "transcript_path": "/dev/null",
        }
        return self.cli("stall", "--hook", "claude-code", "--agent", "claude-code", input=json.dumps(payload))

    # -- hooks -------------------------------------------------------------------------

    def _hook(self, verb: str, payload: dict[str, Any]) -> Result:
        base = {"session_id": self.client_session, "cwd": str(self.cwd), "transcript_path": "/dev/null"}
        return self.run(
            [PY, "-I", str(self.host.layout.gate_script), verb, "--hook", "claude-code"], input=json.dumps({**base, **payload})
        )

    def start(self, *, source: str = "startup") -> str:
        """SessionStart: ``remembra-crew start`` joins through crewd; returns the injected context."""
        env_file = self.host.tmp / f"{self.client_session}.env"
        env_file.write_text("")
        payload = {
            "session_id": self.client_session,
            "cwd": str(self.cwd),
            "source": source,
            "transcript_path": "/dev/null",
            "hook_event_name": "SessionStart",
        }
        res = self.run(
            [
                PY,
                "-m",
                "remembra.relay.crew.cli",
                "start",
                "--hook",
                "claude-code",
                "--agent",
                "claude-code",
                "--agent-pid",
                str(self.pid),
            ],
            input=json.dumps(payload),
            env=self.host.env({"CLAUDE_ENV_FILE": str(env_file)}),
        )
        assert res.rc == 0, res.stderr
        ctx = str(json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"])
        assert "unavailable" not in ctx.split("\n", 1)[0], ctx
        return ctx

    def pretool(self, tool: str, tool_input: dict[str, Any]) -> Result:
        return self._hook(
            "pretool",
            {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "permission_mode": "default"},
        )

    def posttool(self, tool: str, tool_input: dict[str, Any], response: dict[str, Any] | None = None) -> Result:
        return self._hook(
            "posttool",
            {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input, "tool_response": response or {}},
        )

    def bash(self, command: str) -> tuple[Result, Result | None]:
        """PreToolUse, then (when allowed) the command itself and PostToolUse: one Bash tool call."""
        pre = self.pretool("Bash", {"command": command})
        if pre.denied:
            return pre, None
        ran = self.shell(command)
        self.posttool("Bash", {"command": command}, {"stdout": ran.stdout, "stderr": ran.stderr})
        return pre, ran

    def write(self, path: Path, content: str) -> Result:
        """One Write tool call: PreToolUse, then the write and PostToolUse when allowed."""
        pre = self.pretool("Write", {"file_path": str(path), "content": content})
        if not pre.denied:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            self.posttool("Write", {"file_path": str(path), "content": content})
        return pre

    def turn(self, prompt: str = "continue") -> Result:
        return self._hook("turn", {"hook_event_name": "UserPromptSubmit", "prompt": prompt})

    def stop(self, last: str = "All tests pass. The task is done.") -> Result:
        return self._hook("stop", {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": last})

    def session(self) -> dict[str, Any]:
        sess = read_json(self.host.layout.session_file(session_key("claude-code", self.client_session)))
        assert sess, f"no local session for {self.client_session}"
        return sess

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


class LocalHost:
    def __init__(self, tmp_path: Path, url: str, api_key: str, *, zones: str = ZONES_YML) -> None:
        self.tmp = tmp_path
        self.url = url
        self.api_key = api_key
        self.home = (tmp_path / "home").resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.layout = Layout(self.home)
        vendor_gate(self.layout, crewd_argv=[PY, "-m", "remembra.relay.crew.crewd"])
        self.repo = make_repo(tmp_path / "repo", zones=zones, remote=tmp_path / "origin.git")
        self.wt: dict[str, Path] = {}
        self.agents: list[Agent] = []

    def worktree(self, name: str) -> Path:
        if name not in self.wt:
            self.wt[name] = add_worktree(self.repo, self.tmp / f"wt-{name}", f"agent-{name}")
        return self.wt[name]

    def agent(self, name: str, checkout: Path | None = None) -> Agent:
        a = Agent(self, name, checkout or self.worktree(name))
        self.agents.append(a)
        return a

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "CLAUDE", "ANTHROPIC_"))}
        env.update(
            {
                "HOME": str(self.home),
                "REMEMBRA_URL": self.url,
                "REMEMBRA_API_KEY": self.api_key,
                "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        env.update(extra or {})
        return env

    def connect(self, *, scope: str = "global", repos: list[Path] | None = None, git_hooks: bool = True) -> str:
        """``remembra-crew connect --apply --yes`` into the temp HOME, confirmed at a pty (no service)."""
        repos = repos or [self.repo]
        argv = [
            PY, "-m", "remembra.relay.crew.cli", "connect", "--apply", "--yes", "--scope", scope,
            "--agent", "claude-code", "--no-service", "--python", PY,
            "--crew-command", f"{PY} -m remembra.relay.crew.cli",
            "--crewd-command", f"{PY} -m remembra.relay.crew.crewd",
        ]  # fmt: skip
        for r in repos:
            argv += ["--repo", str(r)]
        if git_hooks:
            argv.append("--git-hooks")
        master, slave = pty.openpty()
        try:
            res = subprocess.run(
                argv, stdin=slave, capture_output=True, text=True, cwd=str(repos[0]), env=self.env(), timeout=120
            )
        finally:
            os.close(slave)
            os.close(master)
        assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
        return res.stdout

    def cli(self, *args: str, stdin: str = "", cwd: Path | None = None, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        """``remembra-crew`` run by a human at a terminal (not inside any agent's process tree)."""
        return subprocess.run(
            [PY, "-m", "remembra.relay.crew.cli", *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=str(cwd or self.home),
            env=self.env(),
            timeout=timeout,
        )

    def crewd_pid(self) -> int | None:
        status = read_json(self.layout.status_file) or {}
        pid = status.get("pid")
        return int(pid) if pid and pid_alive(int(pid)) else None

    def shutdown(self) -> None:
        for a in self.agents:
            a.close()
        pid = self.crewd_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            wait_for(lambda: not pid_alive(pid), timeout=10)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)

    @staticmethod
    def head(checkout: Path) -> str:
        return git(checkout, "rev-parse", "HEAD")
