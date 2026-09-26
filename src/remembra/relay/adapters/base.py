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

from remembra.relay.config_view import canonical_json, config_view
from remembra.relay.handoff import PRE_COMPACT_REASON

RELAY_MARKERS = ("remembra-relay", "remembra.relay")

# Output modes for `remembra-relay brief`:
#   text         plain text on stdout (agent adds stdout to the context)
#   hook-json    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}
#   cursor-json  {"additional_context": ...}
OUTPUT_MODES = ("text", "json", "hook-json", "cursor-json")


@dataclass(frozen=True)
class PayloadMap:
    """Where the agent puts session fields in the hook's stdin JSON (first match wins).

    The end reason is, in order: the API error of a failed turn (``error``,
    else ``error_type``: Claude Code's StopFailure), ``pre-compact:<trigger>``
    for a PreCompact event, else the ``reason`` field (SessionEnd).
    """

    session_id: tuple[str, ...] = ("session_id",)
    cwd: tuple[str, ...] = ("cwd",)
    transcript: tuple[str, ...] = ("transcript_path",)
    reason: tuple[str, ...] = ("reason",)
    env_session_id: tuple[str, ...] = ()
    env_cwd: tuple[str, ...] = ()
    event: tuple[str, ...] = ("hook_event_name",)
    error: tuple[str, ...] = ()
    compact_events: tuple[str, ...] = ()
    trigger: tuple[str, ...] = ("trigger",)

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

        event = first(self.event)
        error = first(self.error)
        if error:
            reason: str | None = error
        elif event and event in self.compact_events:
            trigger = first(self.trigger)
            reason = f"{PRE_COMPACT_REASON}:{trigger}" if trigger else PRE_COMPACT_REASON
        else:
            reason = first(self.reason)
        return {
            "session_id": first(self.session_id) or first_env(self.env_session_id),
            "cwd": first(self.cwd) or first_env(self.env_cwd),
            "transcript": first(self.transcript),
            "reason": reason,
            "event": event,
        }


@dataclass(frozen=True)
class CloseEvent:
    """An extra hook event that also runs ``close`` (the session may go on afterwards).

    ``matcher`` is the hook group's matcher (for StopFailure: which API
    errors); None matches every occurrence.
    """

    event: str
    matcher: str | None = None


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
    transcript_format: str | None = None  # only "claude-jsonl" is parsed
    detect_bins: tuple[str, ...] = ()
    detect_dirs: tuple[str, ...] = ()  # relative to home
    config_source: str | None = None  # prefer this config file for the API key ("claude" | "codex")
    hook_timeout: int | None = None  # seconds, only where the unit is verified
    notes: str = ""
    # Events besides ``end_event`` that write the handoff early (Claude Code:
    # StopFailure on a usage/billing limit, PreCompact). A later close of the
    # same session supersedes it on the server.
    extra_close_events: tuple[CloseEvent, ...] = ()


@dataclass
class Change:
    path: Path
    before: str | None
    after: str
    summary: list[str]
    delete: bool = False  # remove the file (nothing but our own entries was left in it)

    @property
    def changed(self) -> bool:
        if self.delete:
            return self.before is not None
        return self.before != self.after

    def diff(self, mask: Callable[[str], str] | None = None, keys: tuple[str, ...] = ()) -> str:
        """Unified diff for printing, with every secret hidden (see :mod:`remembra.relay.config_view`).

        ``keys`` are values to mask wherever they appear; ``mask`` rewrites each
        line after that. JSON is compared in the layout it is written in; when
        the current file uses another layout, a last line says it is rewritten.
        """

        def lines(text: str | None) -> list[str]:
            out = (config_view(text, self.path, keys) or "").splitlines(keepends=True)
            return [mask(line) for line in out] if mask else out

        tofile = f"{self.path} (deleted)" if self.delete else f"{self.path} (new)"
        after = [] if self.delete else lines(self.after)
        text = "".join(difflib.unified_diff(lines(self.before), after, fromfile=f"{self.path} (current)", tofile=tofile))
        if text and not self.delete and self.before is not None:
            layout = canonical_json(self.before)
            if layout is not None and layout != self.before and self.after == canonical_json(self.after):
                text += "(the file is rewritten with 2-space JSON indentation; the content changes only as shown)\n"
        return text


def relay_command() -> str:
    """Absolute command for hooks (hooks run with a minimal PATH)."""
    found = shutil.which("remembra-relay")
    if found:
        return shlex.quote(str(Path(found).resolve()))
    return f"{shlex.quote(sys.executable)} -m remembra.relay.cli"


# Our hook entries end with "<verb> --hook <adapter> --agent <id>", whatever the binary path is.
_RELAY_ARGS_RE = re.compile(r"\s(?:brief|close) --hook [\w.-]+ --agent \S+$")


def is_relay_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    return any(marker in command for marker in RELAY_MARKERS) or bool(_RELAY_ARGS_RE.search(command.strip()))


def _backup(path: Path, stamp: str, label: str) -> Path:
    """Copy ``path`` next to itself, owner-only (it may hold a key), never over an older backup."""
    backup = path.with_name(f"{path.name}.bak-{label}-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.bak-{label}-{stamp}-{n}")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, path.open("rb") as src:
        shutil.copyfileobj(src, out)
    os.chmod(backup, 0o600)
    return backup


def backup_and_write(change: Change, stamp: str | None = None, *, label: str = "relay", private: bool = False) -> Path | None:
    """Back up the current file (if any), then write atomically (or delete, for ``change.delete``).

    A new file is created 0600. An existing file keeps its mode, except with
    ``private`` (the file holds an API key), where group and other access is
    removed. Backups are always 0600. Returns the backup path, if any.
    """
    path = change.path
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup: Path | None = None
    mode: int | None = None
    if path.exists():
        backup = _backup(path, stamp, label)
        mode = path.stat().st_mode & 0o777
        if private:
            mode &= 0o700
    if change.delete:
        if path.exists():
            path.unlink()
        return backup
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(change.after)
            fh.flush()
            os.fsync(fh.fileno())
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
        return {"start": base.format(verb="brief"), "end": base.format(verb="close")}

    def hook_events(self) -> list[tuple[str, str, str | None]]:
        """``(command key, event, matcher)`` for every hook this adapter installs."""
        events: list[tuple[str, str, str | None]] = []
        if self.spec.start_event:
            events.append(("start", self.spec.start_event, None))
        if self.spec.end_event:
            events.append(("end", self.spec.end_event, None))
        events.extend(("end", extra.event, extra.matcher) for extra in self.spec.extra_close_events)
        return events

    def plan(self, home: Path, relay: str) -> Change:
        path = self.spec.config_path(home)
        before = path.read_text(encoding="utf-8") if path.exists() else None
        after, summary = self.render(before, relay)
        return Change(path=path, before=before, after=after, summary=summary)

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        raise NotImplementedError

    def plan_removal(self, home: Path) -> Change:
        """The config without any relay hook (``disconnect``); unchanged when there is none."""
        path = self.spec.config_path(home)
        before = path.read_text(encoding="utf-8") if path.exists() else None
        if before is None:
            return Change(path=path, before=None, after="", summary=[], delete=True)  # nothing to remove
        after, summary, empty = self.render_removal(before)
        return Change(path=path, before=before, after=after, summary=summary, delete=empty and bool(summary))

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        """``(new text, summary, nothing else left)``."""
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

    def _entry(self, command: str) -> dict[str, Any]:
        hook: dict[str, Any] = {"type": "command", "command": command}
        if self.spec.hook_timeout:
            hook["timeout"] = self.spec.hook_timeout
        return {"hooks": [hook]}

    def _group(self, command: str, matcher: str | None) -> dict[str, Any]:
        group = self._entry(command)
        return {"matcher": matcher, **group} if matcher else group

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        data = _load_json_object(before, str(self.spec.config_path))
        new = copy.deepcopy(data)
        hooks = new.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("'hooks' in the config is not an object")
        summary: list[str] = []
        commands = self.commands(relay)
        for key, event, matcher in self.hook_events():
            groups = hooks.get(event)
            groups = list(groups) if isinstance(groups, list) else []
            wanted = self._entry(commands[key])["hooks"][0]
            kept: list[Any] = []
            present = False
            for group in groups:
                inner = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(inner, list):
                    kept.append(group)
                    continue
                same_matcher = (group.get("matcher") or None) == matcher
                remaining = []
                for hook in inner:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if is_relay_command(command):
                        if hook == wanted and same_matcher and not present:
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
                kept.append(self._group(commands[key], matcher))
                on = f" (matcher `{matcher}`)" if matcher else ""
                summary.append(f"{event}{on}: add `{commands[key]}`")
            hooks[event] = kept
        text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
        if before is not None and new == data:
            text = before  # untouched: keep the user's exact formatting
        return text, summary

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        data = _load_json_object(before, str(self.spec.config_path))
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return before, [], False
        new = copy.deepcopy(data)
        new_hooks: dict[str, Any] = new["hooks"]
        summary: list[str] = []
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            kept: list[Any] = []
            for group in groups:
                inner = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(inner, list):
                    kept.append(group)
                    continue
                remaining = []
                for hook in inner:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if is_relay_command(command):
                        summary.append(f"{event}: remove `{command}`")
                    else:
                        remaining.append(hook)
                if remaining:
                    kept.append({**group, "hooks": remaining})
                elif not inner:
                    kept.append(group)  # an empty group that was already there
            if kept:
                new_hooks[event] = kept
            else:
                del new_hooks[event]
        if not summary:
            return before, [], False
        if not new_hooks:
            del new["hooks"]
        return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary, not new
