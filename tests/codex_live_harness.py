"""Run the real Codex CLI against local stand-ins, with no credentials.

- :class:`MockResponses` is a local OpenAI Responses API. Codex talks to it
  through a ``[model_providers.mock]`` entry in a temp ``config.toml``; it
  answers each turn from a script keyed by the prompt (run a shell command,
  reply, or fail with Codex's ``usage_limit_reached`` 429) and keeps every
  request so a test can see what reached the model (the SessionStart brief
  arrives as a developer message).
- :func:`trust_hooks` does what ``/hooks`` does in the Codex TUI: it asks
  ``codex app-server`` (``hooks/list``) for each hook's key and current hash
  and records ``[hooks.state."<key>"] trusted_hash`` in the temp config.toml.
- :func:`find_codex` finds a runnable binary: ``REMEMBRA_CODEX_BIN``, ``codex``
  on PATH, or the one bundled in ChatGPT.app on macOS.

Everything runs under a temp HOME / CODEX_HOME; the user's ~/.codex is never
read or written. See tests/test_relay_codex_live.py.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

CHATGPT_APP_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex"


def find_codex() -> tuple[str, str] | None:
    """(path, version) of a Codex binary that runs, or None."""
    candidates = [os.environ.get("REMEMBRA_CODEX_BIN"), shutil.which("codex"), CHATGPT_APP_CODEX]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            out = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        match = re.search(r"codex-cli (\S+)", out.stdout)
        if out.returncode == 0 and match:
            return candidate, match.group(1)
    return None


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class MockResponses:
    """A local Responses API that plays a script per prompt.

    ``scripts[prompt]`` is a list of steps for that turn: ``("exec", "cmd")``
    asks Codex to run a shell command, ``("say", "text")`` ends the turn with a
    message, ``("limit",)`` answers 429 ``usage_limit_reached``. The step is
    chosen by how many tool results the turn already has.
    """

    def __init__(self, scripts: dict[str, list[tuple[str, ...]]]) -> None:
        self.scripts = scripts
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # /v1/models
                self._send(200, b'{"data": [], "models": []}', "application/json")

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                with owner.lock:
                    owner.requests.append(body)
                status, payload, content_type = owner.answer(body)
                self._send(status, payload, content_type)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> MockResponses:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def prompt_of(body: dict[str, Any]) -> tuple[str | None, int]:
        """(last user prompt, tool results after it) for a request body."""
        items = [i for i in body.get("input") or [] if isinstance(i, dict)]
        last_user, results = None, 0
        for item in items:
            if item.get("type") == "message" and item.get("role") == "user":
                texts = [c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)]
                text = "\n".join(texts).strip()
                if text and not text.startswith("<environment_context>"):
                    last_user, results = text, 0
            elif item.get("type") in ("function_call_output", "custom_tool_call_output"):
                results += 1
        return last_user, results

    @staticmethod
    def developer_texts(body: dict[str, Any]) -> list[str]:
        out = []
        for item in body.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "developer":
                out.extend(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        return out

    def answer(self, body: dict[str, Any]) -> tuple[int, bytes, str]:
        prompt, done = self.prompt_of(body)
        steps = self.scripts.get(prompt or "", [("say", "Nothing to do.")])
        step = steps[min(done, len(steps) - 1)]
        rid = f"resp_{len(self.requests)}"
        if step[0] == "limit":
            error = {
                "error": {
                    "type": "usage_limit_reached",
                    "message": "The usage limit has been reached",
                    "plan_type": "plus",
                    "resets_at": int(time.time()) + 3600,
                }
            }
            return 429, json.dumps(error).encode(), "application/json"
        tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        if step[0] == "exec":
            if "exec_command" in tools:
                name, args = "exec_command", {"cmd": step[1]}
            elif "shell_command" in tools:
                name, args = "shell_command", {"command": step[1]}
            else:
                name, args = "shell", {"command": ["bash", "-lc", step[1]]}
            call = f"call_{len(self.requests)}"
            item: dict[str, Any] = {
                "type": "function_call",
                "id": f"fc_{call}",
                "call_id": call,
                "name": name,
                "arguments": json.dumps(args),
            }
        else:
            item = {
                "type": "message",
                "role": "assistant",
                "id": f"msg_{rid}",
                "content": [{"type": "output_text", "text": step[1]}],
            }
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        events = [
            {"type": "response.created", "response": {"id": rid}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": rid, "usage": usage}},
        ]
        return 200, _sse(events), "text/event-stream"


def write_codex_config(codex_home: Path, base_url: str) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    config = codex_home / "config.toml"
    config.write_text(
        "\n".join(
            [
                'model = "mock-model"',
                'model_provider = "mock"',
                'approval_policy = "never"',
                'sandbox_mode = "workspace-write"',
                "",
                "[model_providers.mock]",
                'name = "local stand-in for the Responses API"',
                f'base_url = "{base_url}"',
                'wire_api = "responses"',
                "",
            ]
        )
    )
    return config


def list_hooks(codex: str, env: dict[str, str], cwd: Path, timeout: float = 60) -> list[dict[str, Any]]:
    """``hooks/list`` from ``codex app-server`` (JSON-RPC over stdio)."""
    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=str(cwd),
    )
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + timeout

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict[str, Any]:
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed its output")
            message = json.loads(line)
            if message.get("id") == request_id:
                return dict(message)
        raise TimeoutError("codex app-server did not answer")

    try:
        send({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "remembra-relay-test", "version": "1"}}})
        receive(1)
        send({"method": "initialized"})
        send({"method": "hooks/list", "id": 2, "params": {"cwds": [str(cwd)]}})
        result = receive(2)["result"]
        return [hook for entry in result["data"] for hook in entry["hooks"]]
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(10)


def trust_hooks(codex: str, env: dict[str, str], cwd: Path, config: Path, events: set[str] | None = None) -> list[dict[str, Any]]:
    """Record trust for the listed hooks (all untrusted ones, or only ``events``), as /hooks does."""
    hooks = list_hooks(codex, env, cwd)
    chosen = [h for h in hooks if h["trustStatus"] != "trusted" and (events is None or h["eventName"] in events)]
    with config.open("a") as fh:
        for hook in chosen:
            fh.write(f"\n[hooks.state.{json.dumps(hook['key'])}]\ntrusted_hash = {json.dumps(hook['currentHash'])}\n")
    return chosen


def run_codex(codex: str, env: dict[str, str], repo: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [codex, "exec", "--skip-git-repo-check", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def newest_rollout(codex_home: Path) -> Path:
    rollouts = sorted((codex_home / "sessions").rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    assert rollouts, "codex wrote no rollout"
    return rollouts[-1]


# ---------------------------------------------------------------------------
# Fixture recording: temp paths become stable placeholders
# ---------------------------------------------------------------------------

FIXTURE_HOME = "/home/dev"
FIXTURE_REPO = "/home/dev/work/widget"
_BULKY_TEXT = ("<skills_instructions>", "<permissions instructions>", "<collaboration_mode>", "<apps_instructions>")


def scrub_text(text: str, replacements: dict[str, str]) -> str:
    for old, new in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    return text


def scrub_rollout(text: str, replacements: dict[str, str]) -> str:
    """Stable paths, and Codex's long built-in instructions cut (they are not relay data)."""
    lines = []
    for raw in text.splitlines():
        entry = json.loads(raw)
        payload = entry.get("payload")
        if isinstance(payload, dict):
            if "base_instructions" in payload:
                payload["base_instructions"] = {"text": "[trimmed: Codex built-in instructions]"}
            if entry.get("type") in ("world_state", "turn_context") or payload.get("type") == "thread_settings_applied":
                entry["payload"] = {k: v for k, v in payload.items() if k in ("turn_id", "cwd", "model", "full")}
            for block in payload.get("content") or []:
                if isinstance(block, dict) and str(block.get("text", "")).lstrip().startswith(_BULKY_TEXT):
                    block["text"] = "[trimmed: Codex built-in developer instructions]"
        lines.append(scrub_text(json.dumps(entry, ensure_ascii=False), replacements))
    return "\n".join(lines) + "\n"
