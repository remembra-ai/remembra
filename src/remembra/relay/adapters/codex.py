"""OpenAI Codex CLI (UNVERIFIED): ``~/.codex/hooks.json`` SessionStart / SessionEnd.

Reported (not verified locally — the installed codex binary would not run):
hooks.json uses the Claude-compatible nesting; stdin JSON has ``session_id``
and ``cwd``. Codex rollout transcripts are a different format and are NOT
parsed; close-outs use git facts only.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

SPEC = AdapterSpec(
    name="codex",
    display="OpenAI Codex CLI",
    verified=False,
    config_path=lambda home: Path(home) / ".codex" / "hooks.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=(), reason=("reason",)),
    output="text",
    transcript_format=None,
    detect_bins=("codex",),
    detect_dirs=(".codex",),
    config_source="codex",
    notes="Unverified: hook file name, event names and payload fields come from research, not the installed tool.",
)

ADAPTER = JsonHooksAdapter(SPEC)


# ---------------------------------------------------------------------------
# Crew mode (§8.3): UNVERIFIED, observe only until `remembra-crew verify` passes
# ---------------------------------------------------------------------------

# Research-grade: Claude-compatible events in ~/.codex/hooks.json. PreToolUse is reported to fire for the
# shell tool only (apply_patch is likely not intercepted), so Codex relies on the read-only fence (§8.3).
# No matchers and no timeouts: the gate filters tools itself and the timeout unit is unverified.
CREW = CrewSpec(
    adapter="codex",
    verified=False,
    style="json-hooks",
    hooks=(
        CrewHook("SessionStart", "start", "cli"),
        CrewHook("UserPromptSubmit", "turn", "gate"),
        CrewHook("PreToolUse", "pretool", "gate"),
        CrewHook("PostToolUse", "posttool", "gate"),
        CrewHook("Stop", "stop", "gate"),
        CrewHook("SessionEnd", "end", "cli"),
    ),
    output="text",
    pretool_events=("PreToolUse",),
    file_gate=False,
    notes="Unverified: Codex hook events and payloads are from research; apply_patch is likely not gated (fence instead).",
)
