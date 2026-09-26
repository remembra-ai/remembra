"""Cursor, IDE and cursor-agent CLI (UNVERIFIED): ``~/.cursor/hooks.json`` sessionStart / sessionEnd.

Docs: https://cursor.com/docs/agent/hooks and https://cursor.com/docs/cli/changelog
(accessed 2026-09-26). Run: cursor-agent 2026.09.26-dd393fe (the package the
vendor's install script downloads), whose own hook code (the config loader and
hook executor from its bundle) was driven by a harness exactly as the CLI's
run loop calls it; a logged-in session could not be run, so the adapter stays
unverified. The payloads in tests/fixtures/relay/cursor/ are that run's.

- ``{"version": 1, "hooks": {"sessionStart": [{"command": ..., "timeout": N}]}}``,
  ``timeout`` in seconds. Both the IDE and the cursor-agent CLI run this file.
  Cursor reads it as JSON with comments.
- Every hook gets ``conversation_id``, ``generation_id``, ``model``,
  ``hook_event_name``, ``cursor_version``, ``workspace_roots``,
  ``user_email`` and ``transcript_path`` (null in the CLI run), and no
  ``cwd``. sessionStart and sessionEnd add ``session_id`` (equal to
  ``conversation_id``); sessionEnd adds ``reason`` and ``final_status``
  (completed / aborted / error in the CLI; the IDE docs add window_close /
  user_close) and ``duration_ms``. Hooks run in ``~/.cursor`` and get
  ``CURSOR_PROJECT_DIR`` (the workspace root), ``CURSOR_VERSION`` and
  ``CLAUDE_PROJECT_DIR``.
- sessionStart may return ``{"additional_context": ...}``. The CLI fires it for
  new chats only (not ``--resume`` / ``--continue``) and waits for that context
  before its first request, so the brief reaches the first turn. The IDE docs
  still call sessionStart fire-and-forget: there the brief may miss the first
  turn. ``close`` detaches, so a closing window does not cut it off, and prints
  ``{}``: Cursor logs a hook with empty stdout as failed.
- Cursor also runs the user's Claude Code hooks (``~/.claude/settings.json``)
  with this same payload; the relay files those under ``cursor``, not
  ``claude-code`` (:mod:`remembra.relay.hosts`, marker ``cursor_version``).
  Claude's SessionStart / SessionEnd / PreCompact run as Cursor's
  sessionStart / sessionEnd / preCompact; preCompact carries ``trigger``
  (auto / manual), so that close is filed as saved before a compaction.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap, _load_json_object, is_relay_command

SPEC = AdapterSpec(
    name="cursor",
    display="Cursor (IDE + cursor-agent)",
    verified=False,
    config_path=lambda home: Path(home) / ".cursor" / "hooks.json",
    start_event="sessionStart",
    end_event="sessionEnd",
    payload=PayloadMap(
        session_id=("session_id", "conversation_id"),
        cwd=("workspace_roots", "cwd"),
        transcript=("transcript_path",),
        reason=("reason",),
        env_cwd=("CURSOR_PROJECT_DIR",),
        # Cursor runs Claude Code's PreCompact hook as its preCompact (with ``trigger``): routed
        # here, that close is filed as "pre-compact:<trigger>", a session still open.
        compact_events=("preCompact", "PreCompact"),
    ),
    output="cursor-json",
    detect_bins=("cursor-agent", "cursor"),
    detect_dirs=(".cursor",),
    hook_timeouts={"start": 15, "end": 15},
    timeout_unit="s",
    detach_close=True,
    notes=(
        "Unverified: cursor-agent 2026.09.26's own hook runner fired these hooks (brief returned, close stored), "
        "but no logged-in Cursor session has run them yet. The CLI gives a brief to new chats only, not --resume."
    ),
)


class CursorHooksAdapter(Adapter):
    def _entry(self, key: str, command: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"command": command}
        timeout = self.spec.timeout_value(key)
        if timeout:
            entry["timeout"] = timeout
        return entry

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        data = _load_json_object(before, f"the {self.spec.display} config")
        new: dict[str, Any] = copy.deepcopy(data)
        new.setdefault("version", 1)
        hooks = new.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("'hooks' in the config is not an object")
        summary: list[str] = []
        commands = self.commands(relay)
        for key, event in self.events():
            current = hooks.get(event)
            entries: list[Any] = current if isinstance(current, list) else []
            kept = [e for e in entries if not (isinstance(e, dict) and is_relay_command(e.get("command")))]
            ours = [e for e in entries if isinstance(e, dict) and is_relay_command(e.get("command"))]
            wanted = self._entry(key, commands[key])
            if ours != [wanted]:
                summary.append(f"{event}: set `{commands[key]}`")
            hooks[event] = [*kept, wanted]
        if before is not None and new == data:
            return before, summary
        return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        data = _load_json_object(before, f"the {self.spec.display} config")
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return before, [], False
        new: dict[str, Any] = copy.deepcopy(data)
        new_hooks: dict[str, Any] = new["hooks"]
        summary: list[str] = []
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            kept = [e for e in entries if not (isinstance(e, dict) and is_relay_command(e.get("command")))]
            summary.extend(f"{event}: remove `{e['command']}`" for e in entries if e not in kept)
            if kept:
                new_hooks[event] = kept
            else:
                del new_hooks[event]
        if not summary:
            return before, [], False
        if not new_hooks:
            del new["hooks"]
        # connect adds "version": 1 to a new file; alone it means nothing is left.
        empty = not new or new == {"version": 1}
        return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary, empty


ADAPTER = CursorHooksAdapter(SPEC)
