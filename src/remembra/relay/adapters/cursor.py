"""Cursor IDE agent hooks (UNVERIFIED): ``~/.cursor/hooks.json`` sessionStart / sessionEnd.

Docs: https://cursor.com/docs/agent/hooks (accessed 2026-09-25). Not run: no
Cursor build with hooks was available where this was written. The payload
fixtures in tests/fixtures/relay/cursor/ are built from the docs, not recorded.

From the docs:

- ``{"version": 1, "hooks": {"sessionStart": [{"command": ..., "timeout": N}]}}``,
  ``timeout`` in seconds.
- Every hook gets ``conversation_id``, ``generation_id``, ``model``,
  ``hook_event_name``, ``cursor_version``, ``workspace_roots``,
  ``user_email`` and ``transcript_path`` (nullable). sessionStart and
  sessionEnd add ``session_id``; sessionEnd adds ``reason`` (completed /
  aborted / error / window_close / user_close) and ``duration_ms``.
- sessionStart may return ``{"additional_context": ...}``, added to the
  conversation's initial system context. Both hooks are fire-and-forget: the
  agent loop does not wait for them. So the brief can miss the first turn, and
  ``close`` detaches so a closing window does not cut it off.
- Hooks get ``CURSOR_PROJECT_DIR`` (workspace root).

The cursor-agent CLI: the docs say "the Cursor CLI also runs hooks", but it
has not been run here. Treat the CLI as MCP-only (``session_brief`` /
``close_session``) until it is.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap, _load_json_object, is_relay_command

SPEC = AdapterSpec(
    name="cursor",
    display="Cursor IDE (agent hooks)",
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
    ),
    output="cursor-json",
    detect_bins=("cursor-agent", "cursor"),
    detect_dirs=(".cursor",),
    hook_timeouts={"start": 15, "end": 15},
    timeout_unit="s",
    detach_close=True,
    notes=(
        "Unverified: built from the Cursor hook docs and doc-derived payloads; not yet run in Cursor. "
        "The cursor-agent CLI is untested: use the MCP tools there."
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
        data = _load_json_object(before, str(self.spec.config_path))
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
        data = _load_json_object(before, str(self.spec.config_path))
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
