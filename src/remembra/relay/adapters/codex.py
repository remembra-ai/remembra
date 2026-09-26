"""OpenAI Codex CLI (verified): ``~/.codex/hooks.json`` SessionStart / UserPromptSubmit / SessionEnd.

Docs: https://developers.openai.com/codex/hooks (also served at
https://learn.chatgpt.com/docs/hooks), accessed 2026-09-25.

Verified by a live round trip with codex-cli 0.155.0-alpha.16.4 (the binary
bundled in ChatGPT.app) in a temp repository with a temp ``CODEX_HOME``, a local
stand-in for the model API and a local Remembra server; no credentials were
used. See tests/test_relay_codex_live.py; the payloads and rollout it recorded
are in tests/fixtures/relay/codex/. Only that version has been run.

What the run showed (and the docs say):

- hooks.json uses the Claude-compatible nesting. Every hook gets stdin JSON
  with ``session_id``, ``transcript_path``, ``cwd`` and ``hook_event_name``;
  SessionStart adds ``source`` (startup / resume / clear / compact), SessionEnd
  ``reason`` ("other"). Plain stdout from SessionStart and UserPromptSubmit is
  added to the model's context as a developer message.
- Codex runs a user hook only after it is trusted: trust is recorded per hook
  against its hash (``[hooks.state."<key>"] trusted_hash`` in config.toml,
  written by ``/hooks``). Untrusted hooks are skipped without a message, so
  ``connect`` prints the trust step.
- ``timeout`` is in seconds. SessionEnd defaults to 1 s and allows at most 3 s,
  shorter than a close takes, so the end hook detaches (``detach_close``).
- SessionStart does not fire when bare ``codex`` auto-restores a thread
  (openai/codex#24228). The UserPromptSubmit hook runs ``brief --once``, which
  delivers the brief only if this session has not had one.
- Rollouts (``~/.codex/sessions/.../rollout-*.jsonl``) are parsed for
  commands, exit codes, test runs, edited files and a usage-limit stop
  (:func:`remembra.relay.facts.parse_codex_rollout`). Codex has no hook for
  its usage limit (openai/codex#45977), so the transcript tail is read.
- JSON ``additionalContext`` output was rejected on 0.154.0 (openai/codex#45999),
  so the brief stays plain text.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap

TESTED_VERSIONS = ("0.155.0-alpha.16.4",)

SPEC = AdapterSpec(
    name="codex",
    display="OpenAI Codex CLI",
    verified=True,
    config_path=lambda home: Path(home) / ".codex" / "hooks.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    prompt_event="UserPromptSubmit",
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=("transcript_path",), reason=("reason",)),
    output="text",
    transcript_format="codex-rollout-jsonl",
    detect_bins=("codex",),
    detect_dirs=(".codex",),
    config_source="codex",
    hook_timeouts={"start": 15, "prompt": 15, "end": 3},
    timeout_unit="s",
    detach_close=True,
    setup_note=(
        "Open Codex and run /hooks to trust the three remembra-relay hooks (SessionStart, UserPromptSubmit, SessionEnd); "
        "they will not run until you do. Codex asks again whenever a hook's command changes."
    ),
    notes=f"Verified with codex-cli {', '.join(TESTED_VERSIONS)} (live round trip); other versions have not been run.",
)

ADAPTER = JsonHooksAdapter(SPEC)
