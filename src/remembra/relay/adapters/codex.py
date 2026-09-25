"""OpenAI Codex CLI (UNVERIFIED): ``~/.codex/hooks.json`` SessionStart / SessionEnd.

Reported (not verified locally — the installed codex binary would not run):
hooks.json uses the Claude-compatible nesting; stdin JSON has ``session_id``
and ``cwd``. Codex rollout transcripts are a different format and are NOT
parsed; close-outs use git facts only.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, JsonHooksAdapter, PayloadMap

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
