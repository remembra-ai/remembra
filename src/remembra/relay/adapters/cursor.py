"""Cursor agent (UNVERIFIED): ``~/.cursor/hooks.json`` sessionStart / sessionEnd.

Reported shape: ``{"version": 1, "hooks": {"sessionStart": [{"command": ...}]}}``;
payload has ``conversation_id``, ``workspace_roots`` and ``transcript_path``;
sessionStart may return ``{"additional_context": ...}``. Not found in the
installed Cursor 3.20 bundle, so dry-run only by default.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap, _load_json_object, is_relay_command

SPEC = AdapterSpec(
    name="cursor",
    display="Cursor (agent hooks)",
    verified=False,
    config_path=lambda home: Path(home) / ".cursor" / "hooks.json",
    start_event="sessionStart",
    end_event="sessionEnd",
    payload=PayloadMap(
        session_id=("conversation_id", "session_id"),
        cwd=("workspace_roots", "cwd"),
        transcript=(),
        reason=("reason",),
    ),
    output="cursor-json",
    detect_bins=("cursor-agent", "cursor"),
    detect_dirs=(".cursor",),
    notes="Unverified: event names and payload fields are from research, not the installed app.",
)


class CursorHooksAdapter(Adapter):
    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        data = _load_json_object(before, str(self.spec.config_path))
        new: dict[str, Any] = copy.deepcopy(data)
        new.setdefault("version", 1)
        hooks = new.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("'hooks' in the config is not an object")
        summary: list[str] = []
        commands = self.commands(relay)
        for key, event in (("start", self.spec.start_event), ("end", self.spec.end_event)):
            if not event:
                continue
            current = hooks.get(event)
            entries: list[Any] = current if isinstance(current, list) else []
            kept = [e for e in entries if not (isinstance(e, dict) and is_relay_command(e.get("command")))]
            ours = [e for e in entries if isinstance(e, dict) and is_relay_command(e.get("command"))]
            if ours != [{"command": commands[key]}]:
                summary.append(f"{event}: set `{commands[key]}`")
            hooks[event] = [*kept, {"command": commands[key]}]
        if before is not None and new == data:
            return before, summary
        return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary


ADAPTER = CursorHooksAdapter(SPEC)
