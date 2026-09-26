"""Claude Code (verified): ``~/.claude/settings.json`` SessionStart / SessionEnd hooks.

Hook stdin JSON carries ``session_id``, ``transcript_path``, ``cwd``,
``hook_event_name`` and (SessionEnd) ``reason``. SessionStart stdout is added
to the model's context. Transcripts are JSONL and parsed for facts.
The older ``integrations/claude-code/session_start.py`` hook is replaced.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap

SPEC = AdapterSpec(
    name="claude-code",
    display="Claude Code",
    verified=True,
    config_path=lambda home: Path(home) / ".claude" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(),
    output="text",
    transcript_format="claude-jsonl",
    detect_bins=("claude",),
    detect_dirs=(".claude",),
    config_source="claude",
    hook_timeouts={"start": 15, "end": 15},
    notes="Verified against Claude Code hook docs and real JSONL transcripts.",
)


class ClaudeCodeAdapter(JsonHooksAdapter):
    legacy_markers = ("integrations/claude-code/session_start.py",)


ADAPTER = ClaudeCodeAdapter(SPEC)
