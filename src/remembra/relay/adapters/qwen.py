"""Qwen Code (UNVERIFIED, a Gemini CLI fork): ``~/.qwen/settings.json`` SessionStart / SessionEnd.

Docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/
(accessed 2026-09-25). Not run: qwen is not installed on the machine this was
written on. The payload fixtures in tests/fixtures/relay/qwen/ are built from
the docs, not recorded.

From the docs:

- Same nesting as Gemini CLI, but ``timeout`` is in SECONDS (default 60); for
  command hooks a value of 1000 or more is still read as milliseconds. We
  write 15.
- stdin JSON: ``session_id``, ``transcript_path``, ``cwd``,
  ``hook_event_name``, ``timestamp``, ``permission_mode``; SessionStart adds
  ``source``, SessionEnd ``reason``.
- A JSON object on stdout is read as hook output
  (``hookSpecificOutput.additionalContext``); plain text is also added to the
  context on SessionStart. We emit the JSON form.
- Hooks get ``QWEN_PROJECT_DIR`` (and the ``GEMINI_PROJECT_DIR`` /
  ``CLAUDE_PROJECT_DIR`` aliases).
- User-level hooks load regardless of folder trust; project hooks load only
  in a trusted folder. ``connect`` writes the user file.
- SessionEnd is informational and the CLI does not wait, so ``close``
  detaches.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap

SPEC = AdapterSpec(
    name="qwen",
    display="Qwen Code",
    verified=False,
    config_path=lambda home: Path(home) / ".qwen" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(
        session_id=("session_id",),
        cwd=("cwd",),
        transcript=("transcript_path",),
        reason=("reason",),
        env_cwd=("QWEN_PROJECT_DIR",),
    ),
    output="hook-json",
    detect_bins=("qwen",),
    detect_dirs=(".qwen",),
    hook_timeouts={"start": 15, "end": 15},
    timeout_unit="s",
    detach_close=True,
    notes="Unverified: built from the Qwen Code hook docs and doc-derived payloads; not yet run against qwen.",
)

ADAPTER = JsonHooksAdapter(SPEC)
