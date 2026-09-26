"""Qwen Code (UNVERIFIED, Gemini CLI fork): ``~/.qwen/settings.json`` SessionStart / SessionEnd."""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

SPEC = AdapterSpec(
    name="qwen",
    display="Qwen Code",
    verified=False,
    config_path=lambda home: Path(home) / ".qwen" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=(), reason=("reason",)),
    output="hook-json",
    detect_bins=("qwen",),
    detect_dirs=(".qwen",),
    notes="Unverified: qwen is not installed here.",
)

ADAPTER = JsonHooksAdapter(SPEC)


# ---------------------------------------------------------------------------
# Crew mode (§8.3): UNVERIFIED, observe only until `remembra-crew verify` passes
# ---------------------------------------------------------------------------

# Research-grade: Qwen Code reports Claude-compatible hook events. No matchers, no timeouts.
CREW = CrewSpec(
    adapter="qwen",
    verified=False,
    style="json-hooks",
    hooks=(
        CrewHook("SessionStart", "start", "cli"),
        CrewHook("UserPromptSubmit", "turn", "gate"),
        CrewHook("PreToolUse", "pretool", "gate"),
        CrewHook("PostToolUse", "posttool", "gate"),
        CrewHook("Stop", "stop", "gate"),
        CrewHook("PreCompact", "precompact", "gate"),
        CrewHook("SessionEnd", "end", "cli"),
    ),
    output="hook-json",
    pretool_events=("PreToolUse",),
    file_gate=True,
    notes="Unverified: Qwen Code hook events are from research; qwen is not installed here.",
)
