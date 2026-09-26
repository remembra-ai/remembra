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
