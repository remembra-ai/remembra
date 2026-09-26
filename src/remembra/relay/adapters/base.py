"""Adapter interface: how one agent is wired to ``remembra-relay``.

An adapter is declarative (:class:`AdapterSpec`) plus a ``plan`` that turns
the agent's current config file into the new content. ``connect`` shows the
plan as a diff (dry run) and only writes with ``--apply``, after a backup.

To add an agent, create one module in this package that builds an adapter
(usually :class:`JsonHooksAdapter` with a spec) and register it in
``adapters/__init__.py``. Mark it ``verified=False`` until its config schema
and hook payload have been checked against the installed tool; unverified
adapters are dry-run only unless ``connect --include-unverified``.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

RELAY_MARKERS = ("remembra-relay", "remembra.relay")

# Output modes for `remembra-relay brief`:
#   text         plain text on stdout (agent adds stdout to the context)
#   hook-json    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}
#   cursor-json  {"additional_context": ...}
OUTPUT_MODES = ("text", "json", "hook-json", "cursor-json")


@dataclass(frozen=True)
class PayloadMap:
    """Where the agent puts session fields in the hook's stdin JSON (first match wins)."""

    session_id: tuple[str, ...] = ("session_id",)
    cwd: tuple[str, ...] = ("cwd",)
    transcript: tuple[str, ...] = ("transcript_path",)
    reason: tuple[str, ...] = ("reason",)
    env_session_id: tuple[str, ...] = ()
    env_cwd: tuple[str, ...] = ()

    def extract(self, payload: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, str | None]:
        env = environ if environ is not None else dict(os.environ)

        def first(keys: tuple[str, ...]) -> str | None:
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):  # e.g. Cursor workspace_roots
                    value = value[0] if value else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        def first_env(keys: tuple[str, ...]) -> str | None:
            return next((env[k].strip() for k in keys if env.get(k, "").strip()), None)

        return {
            "session_id": first(self.session_id) or first_env(self.env_session_id),
            "cwd": first(self.cwd) or first_env(self.env_cwd),
            "transcript": first(self.transcript),
            "reason": first(self.reason),
        }


@dataclass(frozen=True)
class AdapterSpec:
    name: str  # also the default agent id the hooks declare
    display: str
    verified: bool
    config_path: Callable[[Path], Path]  # home -> config file
    start_event: str | None
    end_event: str | None
    payload: PayloadMap = field(default_factory=PayloadMap)
    output: str = "text"
    transcript_format: str | None = None  # "claude-jsonl" | "codex-rollout-jsonl" (facts.TRANSCRIPT_FORMATS); None: not parsed
    detect_bins: tuple[str, ...] = ()
    detect_dirs: tuple[str, ...] = ()  # relative to home
    config_source: str | None = None  # prefer this config file for the API key ("claude" | "codex")
    # Per-hook timeouts in SECONDS, keyed "start" / "prompt" / "end"; written in the agent's
    # own unit (``timeout_unit``). Only set where the agent documents the field and unit.
    hook_timeouts: dict[str, int] = field(default_factory=dict)
    timeout_unit: str = "s"  # "s" or "ms" (Gemini CLI reads milliseconds: 15 would be 15 ms)
    # A third event that runs `brief --once`: delivers the brief when the start event did
    # not fire (Codex does not fire SessionStart when it auto-restores a thread).
    prompt_event: str | None = None
    # `close` hands the work to a detached process and exits at once: for agents that
    # do not wait for the end hook or kill it after a short timeout.
    detach_close: bool = False
    # Printed by `connect` after writing: a step the user must take before the hooks run.
    setup_note: str = ""
    notes: str = ""

    def timeout_value(self, key: str) -> int | None:
        seconds = self.hook_timeouts.get(key)
        if not seconds:
            return None
        return seconds * 1000 if self.timeout_unit == "ms" else seconds


@dataclass
class Change:
    path: Path
    before: str | None
    after: str
    summary: list[str]

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        before = (self.before or "").splitlines(keepends=True)
        after = self.after.splitlines(keepends=True)
        return "".join(difflib.unified_diff(before, after, fromfile=f"{self.path} (current)", tofile=f"{self.path} (new)"))


def relay_command() -> str:
    """Absolute command for hooks (hooks run with a minimal PATH)."""
    found = shutil.which("remembra-relay")
    if found:
        return shlex.quote(str(Path(found).resolve()))
    return f"{shlex.quote(sys.executable)} -m remembra.relay.cli"


# Our hook entries end with "<verb> --hook <adapter> --agent <id>", whatever the binary path is.
_RELAY_ARGS_RE = re.compile(r"\s(?:brief|close) --hook [\w.-]+ --agent \S+(?: --once)?$")


def is_relay_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    return any(marker in command for marker in RELAY_MARKERS) or bool(_RELAY_ARGS_RE.search(command.strip()))


def backup_and_write(change: Change, stamp: str | None = None) -> Path | None:
    """Back up the current file (if any), then write atomically, keeping its mode."""
    path = change.path
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup: Path | None = None
    mode: int | None = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-relay-{stamp}")
        shutil.copy2(path, backup)
        mode = path.stat().st_mode & 0o777
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(change.after)
        os.chmod(tmp, mode if mode is not None else 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


class Adapter:
    """Base adapter. Subclasses implement :meth:`render`."""

    def __init__(self, spec: AdapterSpec) -> None:
        self.spec = spec

    def detect(self, home: Path, which: Callable[[str], str | None] = shutil.which) -> bool:
        return any(which(b) for b in self.spec.detect_bins) or any((home / d).is_dir() for d in self.spec.detect_dirs)

    def commands(self, relay: str) -> dict[str, str]:
        base = f"{relay} {{verb}} --hook {self.spec.name} --agent {self.spec.name}"
        commands = {"start": base.format(verb="brief"), "end": base.format(verb="close")}
        if self.spec.prompt_event:
            commands["prompt"] = base.format(verb="brief") + " --once"
        return commands

    def events(self) -> list[tuple[str, str]]:
        """(command key, agent event name) for every hook this adapter writes."""
        pairs = [("start", self.spec.start_event), ("prompt", self.spec.prompt_event), ("end", self.spec.end_event)]
        return [(key, event) for key, event in pairs if event]

    def plan(self, home: Path, relay: str) -> Change:
        path = self.spec.config_path(home)
        before = path.read_text(encoding="utf-8") if path.exists() else None
        after, summary = self.render(before, relay)
        return Change(path=path, before=before, after=after, summary=summary)

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        raise NotImplementedError


def _load_json_object(text: str | None, path_hint: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path_hint} is not a JSON object")
    return data


class JsonHooksAdapter(Adapter):
    """``{"hooks": {"<Event>": [{"hooks": [{"type": "command", "command": ...}]}]}}``.

    Claude Code's shape (verified); Gemini CLI / Qwen Code / Codex document
    the same nesting. Our entries are found by the ``remembra-relay`` marker,
    so re-running is idempotent and other hooks are left untouched.
    """

    legacy_markers: tuple[str, ...] = ()

    def _entry(self, key: str, command: str) -> dict[str, Any]:
        hook: dict[str, Any] = {"type": "command", "command": command}
        timeout = self.spec.timeout_value(key)
        if timeout:
            hook["timeout"] = timeout
        return {"hooks": [hook]}

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        data = _load_json_object(before, str(self.spec.config_path))
        new = copy.deepcopy(data)
        hooks = new.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("'hooks' in the config is not an object")
        summary: list[str] = []
        commands = self.commands(relay)
        for key, event in self.events():
            groups = hooks.get(event)
            groups = list(groups) if isinstance(groups, list) else []
            kept: list[Any] = []
            present = False
            for group in groups:
                inner = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(inner, list):
                    kept.append(group)
                    continue
                remaining = []
                for hook in inner:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if is_relay_command(command):
                        if command == commands[key] and not present and hook == self._entry(key, commands[key])["hooks"][0]:
                            present = True
                            remaining.append(hook)
                        else:
                            summary.append(f"{event}: replace outdated relay hook `{command}`")
                        continue
                    if isinstance(command, str) and any(m in command for m in self.legacy_markers):
                        summary.append(f"{event}: remove legacy hook `{command}` (superseded by relay brief)")
                        continue
                    remaining.append(hook)
                if remaining:
                    kept.append({**group, "hooks": remaining})
            if not present:
                kept.append(self._entry(key, commands[key]))
                summary.append(f"{event}: add `{commands[key]}`")
            hooks[event] = kept
        text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
        if before is not None and new == data:
            text = before  # untouched: keep the user's exact formatting
        return text, summary
