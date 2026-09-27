"""Qwen Code (verified, a Gemini CLI fork): ``~/.qwen/settings.json`` session hooks.

Docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/
(accessed 2026-09-25). Verified with Qwen Code 0.24.6 (npm
@qwen-code/qwen-code), interactive in a pseudo-terminal and headless, with a
temp HOME and a local stand-in for the model (an ``openai`` provider); no
credentials were used. See tests/test_relay_qwen_live.py; the payloads it
recorded are in tests/fixtures/relay/qwen/. Only that version has been run.

What the run showed (and the v0.24.6 source says):

- Same nesting as Gemini CLI, but ``timeout`` is in SECONDS (for command
  hooks a value of 1000 or more is read as milliseconds). We write 15.
  Settings files may hold comments and a byte order mark. ``$QWEN_HOME``
  moves ``~/.qwen``.
- stdin JSON: ``session_id``, ``transcript_path``
  (``~/.qwen/projects/<cwd>/chats/<session_id>.jsonl``, Gemini-style records,
  not parsed: close-outs use git facts), ``cwd``, ``hook_event_name``,
  ``timestamp``, ``permission_mode``; SessionStart adds ``source`` (startup /
  resume / clear / ...) and ``model``, SessionEnd ``reason``. Hooks get
  ``QWEN_PROJECT_DIR`` (the cwd) and ``QWEN_CODE_SESSION_ID``;
  ``QWEN_CODE_PROJECT_DIR`` is Qwen's storage directory, not the cwd.
- SessionStart's ``hookSpecificOutput.additionalContext`` reaches the model
  (as ``<qwen:session-start-context>``).
- SessionEnd fires only when an interactive session ends: ``/quit``,
  ``/clear`` (reason clear, then a new session), SIGTERM and SIGHUP. The CLI
  gives the end hooks about 2 s, so ``close`` detaches. One-shot ``qwen -p``
  and positional-prompt runs never fire SessionEnd: they write no handoff.
- A turn that stops on a rate limit or billing error fires StopFailure with
  ``error`` (rate_limit / billing_error / ...), and ``/compress`` fires
  PreCompact with ``trigger`` (manual / auto). Both also run ``close``, so the
  handoff is written when the work stops; a later close of the session
  supersedes it.
- User-level hooks run without a folder trust step.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, CloseEvent, JsonHooksAdapter, PayloadMap
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

TESTED_VERSIONS = ("0.24.6",)

# StopFailure errors that stop the work until the user acts (the ones a user switches agents over).
LIMIT_ERRORS = ("rate_limit", "billing_error")

SPEC = AdapterSpec(
    name="qwen",
    display="Qwen Code",
    verified=True,
    config_path=lambda home: Path(home) / ".qwen" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(
        session_id=("session_id",),
        cwd=("cwd",),
        transcript=("transcript_path",),
        reason=("reason",),
        env_session_id=("QWEN_CODE_SESSION_ID",),
        env_cwd=("QWEN_PROJECT_DIR",),
        error=("error",),
        compact_events=("PreCompact",),
    ),
    output="hook-json",
    detect_bins=("qwen",),
    detect_dirs=(".qwen",),
    hook_timeouts={"start": 15, "end": 15},
    timeout_unit="s",
    detach_close=True,
    home_env="QWEN_HOME",
    extra_close_events=(CloseEvent("StopFailure", "|".join(LIMIT_ERRORS)), CloseEvent("PreCompact")),
    notes=(
        f"Verified with Qwen Code {', '.join(TESTED_VERSIONS)}, interactive and headless, with a local stand-in for"
        " the model; other versions have not been run. A handoff is written when an interactive session ends, on a"
        " rate-limit or billing stop, and before /compress; one-shot `qwen -p` runs write none."
    ),
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
