"""A model-free stand-in for the Claude Code process, driving the real installed hooks (WP-15, §13.3).

Run through a symlink named ``claude`` (so crewd's process-tree lookup finds it exactly as it finds the
real binary)::

    <tmp>/bin/claude /abs/path/fake_claude.py --settings S --cwd DIR [--env-file F]

Stdlib only. It reads JSON requests on stdin, one per line, and answers one JSON line each:

* ``{"op": "hook", "event": E, "payload": {...}}`` runs every hook the settings file wires to ``E``
  (matchers as Claude Code applies them), synchronously, the way Claude Code 2.1.168 does
  (``/bin/sh -c <command>``, payload on stdin, ``CLAUDE_PROJECT_DIR``; ``CLAUDE_ENV_FILE`` on
  SessionStart only). ``async``/``asyncRewake`` hooks are started and not awaited.
* ``{"op": "tool", "pre": {...}, "post": {...}}`` is one tool call: PreToolUse hooks first; a
  ``permissionDecision: "deny"`` stops it (the tool never runs, PostToolUse never fires: S0
  ``deny``); otherwise the tool runs here (Write, Edit, Bash; a Bash call sees the exports the
  SessionStart hook wrote to ``CLAUDE_ENV_FILE``, as in S0 ``envfile``) and PostToolUse fires
  with the recorded payload shape plus the real ``tool_response``.
* ``{"op": "ping"}`` and ``{"op": "exit"}``.

The payloads themselves come from the S0 captures (``tests/crew/fixtures/captures``) with this
session's id, paths and tool input filled in by the harness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any

MATCH_FIELD = {"SessionStart": "source", "PreCompact": "trigger", "SessionEnd": "reason"}
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


class Agent:
    def __init__(self, settings: str, cwd: str, env_file: str | None) -> None:
        self.settings = settings
        self.cwd = cwd
        self.env_file = env_file
        self.background: list[subprocess.Popen[bytes]] = []

    # -- hooks -----------------------------------------------------------------------------------
    def entries(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            with open(self.settings, encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, ValueError):
            return []
        groups = (config.get("hooks") or {}).get(event) or []
        subject = payload.get("tool_name") if event in TOOL_EVENTS else payload.get(MATCH_FIELD.get(event, ""))
        out: list[dict[str, Any]] = []
        for group in groups:
            matcher = group.get("matcher")
            if matcher not in (None, "", "*"):
                if not isinstance(subject, str) or re.fullmatch(str(matcher), subject) is None:
                    continue
            out.extend(h for h in group.get("hooks") or () if isinstance(h, dict) and h.get("type") == "command")
        return out

    def hook_env(self, event: str) -> dict[str, str]:
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = self.cwd
        env.pop("CLAUDE_ENV_FILE", None)
        if event == "SessionStart" and self.env_file:
            env["CLAUDE_ENV_FILE"] = self.env_file
        return env

    def run_hooks(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        data = json.dumps(payload).encode()
        for hook in self.entries(event, payload):
            command = str(hook.get("command") or "")
            if hook.get("async") or hook.get("asyncRewake"):
                proc = subprocess.Popen(  # noqa: S603
                    ["/bin/sh", "-c", command],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=self.cwd,
                    env=self.hook_env(event),
                )
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(data)
                    proc.stdin.close()
                except OSError:
                    pass
                self.background.append(proc)
                results.append({"command": command, "async": True, "pid": proc.pid})
                continue
            started = time.perf_counter()
            try:
                done = subprocess.run(  # noqa: S603
                    ["/bin/sh", "-c", command],
                    input=data,
                    capture_output=True,
                    cwd=self.cwd,
                    env=self.hook_env(event),
                    timeout=float(hook.get("timeout") or 60),
                )
                code, out, err = done.returncode, done.stdout.decode(), done.stderr.decode()
            except subprocess.TimeoutExpired:
                code, out, err = -1, "", "timeout"
            results.append(
                {
                    "command": command,
                    "exit": code,
                    "stdout": out,
                    "stderr": err[-2000:],
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        self.background = [p for p in self.background if p.poll() is None]
        return results

    # -- tools -------------------------------------------------------------------------------------
    def env_exports(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if not self.env_file or not os.path.exists(self.env_file):
            return out
        with open(self.env_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
                if m:
                    parts = shlex.split(m.group(2))
                    out[m.group(1)] = parts[0] if parts else ""
        return out

    def execute(self, tool: str, tool_input: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if tool == "Write":
            path = str(tool_input["file_path"])
            existed = os.path.exists(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(tool_input.get("content") or ""))
            return {"type": "update" if existed else "create", "filePath": path, "content": tool_input.get("content")}, True
        if tool == "Edit":
            path = str(tool_input["file_path"])
            with open(path, encoding="utf-8") as fh:
                before = fh.read()
            old, new = str(tool_input.get("old_string") or ""), str(tool_input.get("new_string") or "")
            if old not in before:
                return {"error": "old_string not found"}, False
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(before.replace(old, new, 1))
            return {"filePath": path, "oldString": old, "newString": new}, True
        if tool == "Bash":
            env = dict(os.environ)
            env.update(self.env_exports())
            done = subprocess.run(  # noqa: S603
                ["/bin/sh", "-c", str(tool_input.get("command") or "")],
                capture_output=True,
                cwd=self.cwd,
                env=env,
                timeout=120,
            )
            response = {
                "stdout": done.stdout.decode(errors="replace")[-8000:],
                "stderr": done.stderr.decode(errors="replace")[-8000:],
                "interrupted": False,
                "isImage": False,
            }
            if done.returncode != 0:
                response["exit_code"] = done.returncode
            return response, done.returncode == 0
        return {"error": f"fake_claude cannot run {tool}"}, False

    def tool(self, pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
        hooks = self.run_hooks("PreToolUse", pre)
        for h in hooks:
            if h.get("exit") == 2:
                return {"denied": True, "reason": h.get("stderr", ""), "hooks": hooks, "exit2": True}
            text = (h.get("stdout") or "").strip()
            if not text:
                continue
            try:
                decision = (json.loads(text).get("hookSpecificOutput") or {}).get("permissionDecision")
                reason = (json.loads(text).get("hookSpecificOutput") or {}).get("permissionDecisionReason")
            except (ValueError, AttributeError):
                continue
            if decision == "deny":
                return {"denied": True, "reason": reason, "hooks": hooks}
        started = time.perf_counter()
        response, ok = self.execute(str(pre.get("tool_name")), dict(pre.get("tool_input") or {}))
        post = dict(post)
        post["duration_ms"] = int((time.perf_counter() - started) * 1000)
        if ok:
            post["tool_response"] = response
            post_hooks = self.run_hooks("PostToolUse", post)
        else:
            # Claude Code 2.1.168 (recorded against the real binary, fixtures/bash_fail): a failed tool, including a
            # Bash command that exits non-zero, fires PostToolUseFailure with ``error`` and no ``tool_response``.
            post.pop("tool_response", None)
            post["hook_event_name"] = "PostToolUseFailure"
            text = "\n".join(t for t in (response.get("stdout"), response.get("stderr"), response.get("error")) if t)
            post["error"] = f"Exit code {response.get('exit_code', 1)}\n{text}".rstrip()
            post["is_interrupt"] = False
            post_hooks = self.run_hooks("PostToolUseFailure", post)
        return {"denied": False, "ok": ok, "response": response, "hooks": hooks, "post_hooks": post_hooks}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--settings", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--env-file")
    args = p.parse_args()
    agent = Agent(args.settings, args.cwd, args.env_file)
    os.chdir(args.cwd)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        op = req.get("op")
        try:
            if op == "ping":
                out: dict[str, Any] = {"pid": os.getpid()}
            elif op == "hook":
                out = {"hooks": agent.run_hooks(str(req["event"]), dict(req.get("payload") or {}))}
            elif op == "run":  # a command inside this process tree without hooks (a user's own shell)
                env = dict(os.environ)
                env.update(agent.env_exports())
                done = subprocess.run(  # noqa: S603
                    ["/bin/sh", "-c", str(req["command"])], capture_output=True, cwd=agent.cwd, env=env, timeout=120
                )
                out = {"exit": done.returncode, "stdout": done.stdout.decode(), "stderr": done.stderr.decode()}
            elif op == "write_raw":  # a tool the agent's hooks never see (Codex apply_patch, §8.3)
                try:
                    with open(str(req["path"]), "w", encoding="utf-8") as fh:
                        fh.write(str(req.get("content") or ""))
                    out = {"written": True}
                except OSError as e:
                    out = {"written": False, "errno": e.errno, "exception": type(e).__name__}
            elif op == "tool":
                out = agent.tool(dict(req["pre"]), dict(req.get("post") or {}))
            elif op == "exit":
                sys.stdout.write(json.dumps({"id": req.get("id"), "ok": True}) + "\n")
                sys.stdout.flush()
                return 0
            else:
                out = {"error": f"unknown op {op}"}
            out["id"] = req.get("id")
        except Exception as e:  # report, keep serving
            out = {"id": req.get("id"), "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
