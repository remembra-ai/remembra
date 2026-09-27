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

from remembra.crew.schemas import CREW_HOOK_MARKER
from remembra.relay.config_view import canonical_json, config_view, loads_jsonc
from remembra.relay.handoff import PRE_COMPACT_REASON

RELAY_MARKERS = ("remembra-relay", "remembra.relay")

# Output modes for `remembra-relay brief`:
#   text                     plain text on stdout (agent adds stdout to the context)
#   hook-json                {"hookSpecificOutput": {"hookEventName": <the payload's event>, "additionalContext": ...}}
#   cursor-json              {"additional_context": ...}; `close` prints {} (Cursor logs empty stdout as a failed hook)
#   additional-context-json  {"additionalContext": ...} (GitHub Copilot CLI)
OUTPUT_MODES = ("text", "json", "hook-json", "cursor-json", "additional-context-json")


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
    transcript_format: str | None = None  # "claude-jsonl" | "codex-rollout-jsonl" (facts.TRANSCRIPT_FORMATS); None: not parsed
    detect_bins: tuple[str, ...] = ()
    detect_dirs: tuple[str, ...] = ()  # relative to home
    # Files that mean "installed" (relative to home), where a directory alone would not: Gemini
    # CLI's ~/.gemini is also Antigravity's, but only Gemini CLI writes ~/.gemini/settings.json.
    detect_files: tuple[str, ...] = ()
    config_source: str | None = None  # prefer this config file for the API key ("claude" | "codex")
    # Per-hook timeouts in SECONDS, keyed "start" / "prompt" / "end"; written in the agent's
    # own unit (``timeout_unit``). Only set where the agent documents the field and unit.
    hook_timeouts: dict[str, int] = field(default_factory=dict)
    timeout_unit: str = "s"  # "s" or "ms" (Gemini CLI reads milliseconds: 15 would be 15 ms)
    # A third event that runs `brief --once`: delivers the brief when the start event did
    # not fire (Codex does not fire SessionStart when it auto-restores a thread).
    prompt_event: str | None = None
    # Start ``source`` values whose output the agent throws away (Gemini CLI's /clear):
    # `brief` does nothing then, so the prompt event's `brief --once` delivers it.
    start_sources_without_context: tuple[str, ...] = ()
    # Hook events whose brief a resumed session's restored history still holds. A start with
    # source ``resume`` prints nothing when this session's brief came from one of them (Codex,
    # Kimi Code), and prints it again when it came from another (Gemini CLI keeps what
    # BeforeAgent added to a prompt, but not its SessionStart context; Claude Code: none).
    resume_keeps_brief_from: tuple[str, ...] = ()
    # False when the agent may send the first prompt, and run the prompt hook, before its start
    # hook has finished (Gemini CLI's interactive UI runs SessionStart in the background, and
    # ``gemini -i`` runs the first prompt's BeforeAgent alongside it). The start hook then skips a
    # session whose brief was printed already, and of two hooks printing at once only the first does.
    start_awaited: bool = True
    # `close` hands the work to a detached process and exits at once: for agents that
    # do not wait for the end hook or kill it after a short timeout.
    detach_close: bool = False
    # Printed by `connect` after writing: a step the user must take before the hooks run.
    setup_note: str = ""
    notes: str = ""
    # Events besides ``end_event`` that write the handoff early (Claude Code:
    # StopFailure on a usage/billing limit, PreCompact). A later close of the
    # same session supersedes it on the server.
    extra_close_events: tuple[CloseEvent, ...] = ()
    # Environment variable that moves the agent's own directory (the first part of
    # ``config_path`` under home, e.g. ``~/.qwen``); see :meth:`config_file`.
    home_env: str | None = None
    # ``$home_env`` names a replacement HOME that holds the agent's directory, not the directory
    # itself (Gemini CLI reads ``$GEMINI_CLI_HOME/.gemini/settings.json``).
    home_env_is_home: bool = False
    # Payload keys that mean "not a session of its own" (a subagent's end): the hook does nothing.
    skip_payload_keys: tuple[str, ...] = ()
    # A second close of the same session, event and end reason within this many seconds is
    # dropped (Gemini CLI fires SessionEnd 2-3 times on exit; a session can run several
    # agents' copies of one hook). 0 turns it off. A close with no transcript to measure is
    # dropped only within a few seconds (see ``remembra.relay.cli.claim_close``).
    dedupe_close_seconds: int = 60
    # A close hook with empty stdin does nothing: the agent also runs an orphaned copy of the
    # end hook without its payload (Gemini CLI's third SessionEnd on /quit).
    drop_empty_payload_close: bool = False

    def timeout_value(self, key: str) -> int | None:
        seconds = self.hook_timeouts.get(key)
        if not seconds:
            return None
        return seconds * 1000 if self.timeout_unit == "ms" else seconds

    def moved_home(self, home: Path) -> Path | None:
        """``$home_env`` when it is set, else None.

        Read only for the real home (``Path.home()``): an adapter asked about any
        other home (a test's temporary one) never follows the user's own setting.
        """
        if not self.home_env or Path(home) != Path.home():
            return None
        value = os.environ.get(self.home_env, "").strip()
        return self.dir_from_env(value) if value else None

    def dir_from_env(self, value: str) -> Path:
        """The agent's own directory when ``$home_env`` is ``value`` (see ``home_env_is_home``)."""
        path = Path(value).expanduser()
        if not self.home_env_is_home:
            return path
        try:
            return path / self.config_path(path).relative_to(path).parts[0]
        except (ValueError, IndexError):
            return path

    def config_file(self, home: Path) -> Path:
        """The config file the agent reads: ``config_path(home)``, inside ``$home_env`` when that is set."""
        default = self.config_path(Path(home))
        moved = self.moved_home(home)
        if moved is None:
            return default
        try:
            inside = default.relative_to(home).parts[1:]
        except ValueError:
            return default
        return moved.joinpath(*inside)

    def config_home(self, home: Path) -> Path:
        """The agent's own directory (``~/.claude``, ``~/.codex``, ..., or ``$home_env``)."""
        moved = self.moved_home(home)
        if moved is not None:
            return moved
        default = self.config_path(Path(home))
        try:
            return Path(home) / default.relative_to(home).parts[0]
        except (ValueError, IndexError):
            return default.parent


class RefusedEdit(ValueError):
    """The edit would change more of the user's config than the relay's own entries: nothing is written."""


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
_RELAY_ARGS_RE = re.compile(r"\s(?:brief|close) --hook [\w.-]+ --agent \S+(?: --once)?$")


def is_relay_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    if CREW_HOOK_MARKER in command:  # crew entries (remembra-crew connect) are not relay's to replace
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


def write_target(path: Path) -> Path:
    """The file a write to ``path`` replaces: ``path``, or for a symbolic link the file it points to.

    Replacing the link itself with a regular file would cut it from its target
    (a dotfiles repository, say): the target would never get the change, and
    edits made there later would no longer reach the agent. A link to a file
    that does not exist (or a loop of links) raises ``OSError``: there is no
    file to update, and creating one somewhere the link names is not ours to do.
    """
    if not path.is_symlink():
        return path
    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError as e:
        raise OSError(
            f"{path} is a symbolic link to a file that does not exist ({e.__class__.__name__}); fix or remove the link"
        ) from e


def backup_and_write(change: Change, stamp: str | None = None, *, label: str = "relay", private: bool = False) -> Path | None:
    """Back up the current file (if any), then write atomically (or delete, for ``change.delete``).

    A new file is created 0600. An existing file keeps its mode, except with
    ``private`` (the file holds an API key), where group and other access is
    removed. Backups are always 0600. Returns the backup path, if any.

    A symbolic link is written through (:func:`write_target`): the file it
    points to is replaced and the link stays. It is never deleted either: for
    ``change.delete`` the file it points to gets ``change.after`` (what is left).
    The backup is kept next to the link.
    """
    path = change.path
    target = write_target(path)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup: Path | None = None
    mode: int | None = None
    if target.exists():
        backup = _backup(path, stamp, label)
        mode = target.stat().st_mode & 0o777
        if private:
            mode &= 0o700
    if change.delete and target == path:
        if path.exists():
            path.unlink()
        return backup
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(change.after)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else 0o600)
        os.replace(tmp, target)
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
        spec = self.spec
        if any(which(b) for b in spec.detect_bins) or any((home / d).is_dir() for d in spec.detect_dirs):
            return True
        if any((home / f).is_file() for f in spec.detect_files):
            return True
        moved = spec.moved_home(home)
        return moved is not None and moved.is_dir()

    def commands(self, relay: str) -> dict[str, str]:
        base = f"{relay} {{verb}} --hook {self.spec.name} --agent {self.spec.name}"
        commands = {"start": base.format(verb="brief"), "end": base.format(verb="close")}
        if self.spec.prompt_event:
            commands["prompt"] = base.format(verb="brief") + " --once"
        return commands

    def hook_events(self) -> list[tuple[str, str, str | None]]:
        """``(command key, event, matcher)`` for every hook this adapter installs.

        The start, prompt (``brief --once``) and end events, then the extra
        events that also run ``close`` (with their matcher, if any).
        """
        pairs = [("start", self.spec.start_event), ("prompt", self.spec.prompt_event), ("end", self.spec.end_event)]
        events: list[tuple[str, str, str | None]] = [(key, event, None) for key, event in pairs if event]
        events.extend(("end", extra.event, extra.matcher) for extra in self.spec.extra_close_events)
        return events

    def events(self) -> list[tuple[str, str]]:
        """``(command key, event)`` for every hook this adapter writes (:meth:`hook_events` without matchers)."""
        return [(key, event) for key, event, _ in self.hook_events()]

    def plan(self, home: Path, relay: str) -> Change:
        """The agent's config (:meth:`AdapterSpec.config_file`) with the relay hooks written in."""
        return self.plan_file(self.spec.config_file(home), relay)

    def plan_file(self, path: Path, relay: str) -> Change:
        before = path.read_text(encoding="utf-8") if path.exists() else None
        after, summary = self.render(before, relay)
        return _note_dropped_comments(Change(path=path, before=before, after=after, summary=summary))

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        raise NotImplementedError

    def plan_removal(self, home: Path) -> Change:
        """The config without any relay hook (``disconnect``); unchanged when there is none."""
        return self.plan_file_removal(self.spec.config_file(home))

    def plan_file_removal(self, path: Path) -> Change:
        before = path.read_text(encoding="utf-8") if path.exists() else None
        if before is None:
            return Change(path=path, before=None, after="", summary=[], delete=True)  # nothing to remove
        after, summary, empty = self.render_removal(before)
        # A symbolic link is never removed (backup_and_write writes through it): what is left is written.
        delete = empty and bool(summary) and not path.is_symlink()
        return _note_dropped_comments(Change(path=path, before=before, after=after, summary=summary, delete=delete))

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        """``(new text, summary, nothing else left)``."""
        raise NotImplementedError

    def earlier_files(self, home: Path) -> list[Path]:
        """Files besides :meth:`AdapterSpec.config_file` where an earlier ``connect`` may have written relay hooks.

        The default path when ``$home_env`` moves the agent's directory: earlier
        releases wrote there whatever the variable said, and the agent still reads
        it in a session started without the variable. ``connect`` keeps relay
        hooks found there current; ``disconnect`` removes them.
        """
        default = self.spec.config_path(Path(home))
        return [default] if default != self.spec.config_file(home) else []

    def retired_files(self, home: Path) -> list[Path]:
        """Files an earlier release wrote relay hooks to that the agent no longer reads: ``connect`` and
        ``disconnect`` both remove the relay hooks from them (none by default)."""
        return []

    def hook_files(self, home: Path) -> list[Path]:
        """Every file that may hold this adapter's relay hooks: the config file, then
        :meth:`earlier_files` and :meth:`retired_files`."""
        return list(dict.fromkeys([self.spec.config_file(home), *self.earlier_files(home), *self.retired_files(home)]))

    def connected(self, home: Path) -> bool:
        """True when a file the agent reads (the config file, :meth:`earlier_files`) already holds relay hooks.

        ``connect --apply`` keeps those current. A retired file does not count:
        its hooks say nothing about the agent being installed. A file that cannot
        be read counts as holding none (``connect`` reports it).
        """
        for path in dict.fromkeys([self.spec.config_file(home), *self.earlier_files(home)]):
            try:
                if self.plan_file_removal(path).changed:
                    return True
            except (OSError, ValueError):  # UnicodeDecodeError and the JSON / TOML errors are ValueErrors
                continue
        return False


def _note_dropped_comments(change: Change) -> Change:
    """Add a summary line when writing ``change`` drops the comments of a JSON-with-comments file."""
    if not change.changed or change.delete or change.before is None or change.path.suffix.lower() != ".json":
        return change
    try:
        _, with_comments = loads_jsonc(change.before)
    except ValueError:
        return change
    if with_comments:
        change.summary.append(f"comments in {change.path.name} are not kept; the backup keeps them")
    return change


def _load_json_object(text: str | None, path_hint: str) -> dict[str, Any]:
    """``text`` as a JSON object ({} when empty); a BOM, comments and trailing commas are accepted."""
    if not text or not text.removeprefix("\ufeff").strip():
        return {}
    data, _ = loads_jsonc(text)
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

    def _group(self, key: str, command: str, matcher: str | None) -> dict[str, Any]:
        group = self._entry(key, command)
        return {"matcher": matcher, **group} if matcher else group

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        data = _load_json_object(before, f"the {self.spec.display} config")
        new = copy.deepcopy(data)
        hooks = new.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("'hooks' in the config is not an object")
        summary: list[str] = []
        commands = self.commands(relay)
        for key, event, matcher in self.hook_events():
            groups = hooks.get(event)
            groups = list(groups) if isinstance(groups, list) else []
            wanted = self._entry(key, commands[key])["hooks"][0]
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
                kept.append(self._group(key, commands[key], matcher))
                on = f" (matcher `{matcher}`)" if matcher else ""
                summary.append(f"{event}{on}: add `{commands[key]}`")
            hooks[event] = kept
        text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
        if before is not None and new == data:
            text = before  # untouched: keep the user's exact formatting
        return text, summary

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        data = _load_json_object(before, f"the {self.spec.display} config")
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
