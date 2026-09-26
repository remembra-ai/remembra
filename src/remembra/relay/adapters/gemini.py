"""Gemini CLI (UNVERIFIED): ``~/.gemini/settings.json`` SessionStart / SessionEnd hooks.

Docs: https://geminicli.com/docs/hooks/ and https://geminicli.com/docs/hooks/reference/
(accessed 2026-09-25). Not run: gemini is not installed on the machine this
was written on. The payload fixtures in tests/fixtures/relay/gemini/ are built
from the reference, not recorded.

From the docs:

- Claude-compatible nesting under ``hooks``; each hook has ``type``,
  ``command`` and ``timeout`` in MILLISECONDS (default 60000). We write
  15000; a bare 15 would be 15 ms.
- stdin JSON: ``session_id``, ``transcript_path``, ``cwd``,
  ``hook_event_name``, ``timestamp``; SessionStart adds ``source``, SessionEnd
  ``reason`` (exit / clear / logout / prompt_input_exit / other).
- stdout must be JSON only, so the brief is emitted as
  ``{"hookSpecificOutput": {"additionalContext": ...}}``.
- Hooks also get ``GEMINI_SESSION_ID``, ``GEMINI_CWD`` and ``GEMINI_PROJECT_DIR``.
- SessionEnd is best effort: "the CLI will not wait" for it, so ``close``
  detaches and posts from a background process.
- The transcript is Gemini's own chat format, which is not parsed; close-outs
  use git facts.
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
        transcript=("transcript_path",),
        reason=("reason",),
        env_session_id=("GEMINI_SESSION_ID",),
        env_cwd=("GEMINI_CWD", "GEMINI_PROJECT_DIR"),
    ),
    output="hook-json",
    detect_bins=("gemini",),
    hook_timeouts={"start": 15, "end": 15},
    timeout_unit="ms",
    detach_close=True,
    notes="Unverified: built from the Gemini CLI hook docs and doc-derived payloads; not yet run against gemini.",
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
