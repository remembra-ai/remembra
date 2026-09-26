"""Gemini CLI (verified): ``~/.gemini/settings.json`` SessionStart / BeforeAgent / SessionEnd hooks.

Docs: https://geminicli.com/docs/hooks/ and https://geminicli.com/docs/hooks/reference/
(accessed 2026-09-25). Verified with Gemini CLI 0.61.0 (npm @google/gemini-cli),
headless (``gemini -p``) and interactive in a pseudo-terminal, with a temp
HOME and a local stand-in for the Gemini API (``GOOGLE_GEMINI_BASE_URL``, the
CLI's "gateway" auth); no credentials were used. See
tests/test_relay_gemini_live.py; the payloads it recorded are in
tests/fixtures/relay/gemini/. Only that version has been run.

What the run showed:

- Claude-compatible nesting under ``hooks``; each hook has ``type``,
  ``command`` and ``timeout`` in MILLISECONDS. We write 15000; a bare 15
  would be 15 ms. Settings files may hold comments.
- stdin JSON: ``session_id``, ``transcript_path``
  (``~/.gemini/tmp/<project>/chats/session-<time>-<id8>.jsonl``, Gemini's own
  chat records, not parsed: close-outs use git facts), ``cwd``,
  ``hook_event_name``, ``timestamp``; SessionStart adds ``source`` (startup /
  resume / clear), SessionEnd ``reason`` (exit / clear / ...), BeforeAgent
  ``prompt``. Hooks also get ``GEMINI_SESSION_ID``, ``GEMINI_CWD`` and
  ``GEMINI_PROJECT_DIR``.
- stdout must be JSON only. ``{"hookSpecificOutput": {"additionalContext": ...}}``
  from SessionStart and BeforeAgent reaches the model as ``<hook_context>``
  (with ``<`` and ``>`` HTML-escaped).
- ``/clear`` ends the session (SessionEnd, reason clear) and starts a new one
  (SessionStart, source clear) whose start output is thrown away. The
  BeforeAgent hook runs ``brief --once``, so the new session gets its brief
  with its first prompt; it prints nothing while a session has had one.
- Interactive exit fires SessionEnd two or three times for one session, the
  last copy orphaned with empty stdin: repeats are dropped (the close dedupe)
  and so is the empty one. The CLI waits for SessionEnd, but ``close`` still
  detaches, so a slow server never holds up the exit.
- Every hook, the user-level ones in ``~/.gemini/settings.json`` included,
  runs only in a trusted folder: ``connect`` prints that step. Headless
  ``gemini -p`` in an untrusted folder exits before any hook fires.
- ``$GEMINI_CLI_HOME`` replaces the home directory Gemini keeps ``.gemini``
  in (settings and transcripts); ``connect`` follows it.

Gemini CLI stopped serving personal Google accounts on 2026-06-18; this is for
API-key, Vertex and enterprise users. Antigravity also keeps files in
``~/.gemini``, so Gemini CLI is detected by its binary or its settings file,
not the directory.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap

TESTED_VERSIONS = ("0.61.0",)

SPEC = AdapterSpec(
    name="gemini",
    display="Gemini CLI",
    verified=True,
    config_path=lambda home: Path(home) / ".gemini" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    prompt_event="BeforeAgent",
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
    detect_files=(".gemini/settings.json",),
    home_env="GEMINI_CLI_HOME",  # a replacement HOME: $GEMINI_CLI_HOME/.gemini/settings.json
    home_env_is_home=True,
    hook_timeouts={"start": 15, "prompt": 15, "end": 15},
    timeout_unit="ms",
    start_sources_without_context=("clear",),
    detach_close=True,
    dedupe_close_seconds=60,
    # /quit fires SessionEnd three times, the third orphaned with empty stdin. The repeats are
    # dropped by the close dedupe; the empty one would close without a reason.
    drop_empty_payload_close=True,
    setup_note=(
        "Gemini CLI runs hooks, these user-level ones included, only in trusted folders: choose 'Trust folder'"
        " when Gemini asks in a project (headless `gemini -p` needs --skip-trust or GEMINI_CLI_TRUST_WORKSPACE=true)."
        " In an untrusted folder no brief is loaded and no handoff is saved."
    ),
    notes=(
        f"Verified with Gemini CLI {', '.join(TESTED_VERSIONS)}, headless and interactive, with a local stand-in for"
        " the model; other versions have not been run."
    ),
)

ADAPTER = JsonHooksAdapter(SPEC)
