"""Gemini CLI (UNVERIFIED): ``~/.gemini/settings.json`` SessionStart / SessionEnd hooks.

Reported: Claude-compatible hook nesting; stdout must be JSON only, so the
brief is emitted as ``{"hookSpecificOutput": {"additionalContext": ...}}``;
``GEMINI_SESSION_ID`` / ``GEMINI_CWD`` / ``GEMINI_PROJECT_DIR`` are set in the
hook environment. Transcripts are not parsed.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

SPEC = AdapterSpec(
    name="gemini",
    display="Gemini CLI",
    verified=False,
    config_path=lambda home: Path(home) / ".gemini" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(
        session_id=("session_id",),
        cwd=("cwd",),
        transcript=(),
        reason=("reason",),
        env_session_id=("GEMINI_SESSION_ID",),
        env_cwd=("GEMINI_CWD", "GEMINI_PROJECT_DIR"),
    ),
    output="hook-json",
    detect_bins=("gemini",),
    notes="Unverified: gemini is not installed here (~/.gemini belongs to Antigravity).",
)

ADAPTER = JsonHooksAdapter(SPEC)


# ---------------------------------------------------------------------------
# Crew mode (§8.3): UNVERIFIED, observe only until `remembra-crew verify` passes
# ---------------------------------------------------------------------------

# Research-grade Gemini CLI event names (BeforeAgent = per turn, BeforeTool/AfterTool, AfterAgent = turn end,
# PreCompress). No matchers and no timeouts (Gemini's timeout unit is unverified); the gate filters tools.
CREW = CrewSpec(
    adapter="gemini",
    verified=False,
    style="json-hooks",
    hooks=(
        CrewHook("SessionStart", "start", "cli"),
        CrewHook("BeforeAgent", "turn", "gate"),
        CrewHook("BeforeTool", "pretool", "gate"),
        CrewHook("AfterTool", "posttool", "gate"),
        CrewHook("AfterAgent", "stop", "gate"),
        CrewHook("PreCompress", "precompact", "gate"),
        CrewHook("SessionEnd", "end", "cli"),
    ),
    output="hook-json",
    pretool_events=("BeforeTool",),
    file_gate=True,
    notes="Unverified: Gemini CLI hook events and payload fields are from research, not the installed tool.",
)
