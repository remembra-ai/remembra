"""Qwen Code (UNVERIFIED, Gemini CLI fork): ``~/.qwen/settings.json`` SessionStart / SessionEnd."""

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
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=(), reason=("reason",)),
    output="hook-json",
    detect_bins=("qwen",),
    detect_dirs=(".qwen",),
    notes="Unverified: qwen is not installed here.",
)

ADAPTER = JsonHooksAdapter(SPEC)
