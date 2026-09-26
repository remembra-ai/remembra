"""Claude Code (verified): ``~/.claude/settings.json`` SessionStart / SessionEnd hooks.

Hook stdin JSON carries ``session_id``, ``transcript_path``, ``cwd``,
``hook_event_name`` and (SessionEnd) ``reason``. SessionStart stdout is added
to the model's context. Transcripts are JSONL and parsed for facts.
The older ``integrations/claude-code/session_start.py`` hook is replaced.

When a turn fails on a usage or billing limit the session stays open, so
SessionEnd only fires when the user later quits. StopFailure (matched on
those errors) and PreCompact therefore also run ``close``: the handoff is
written at the moment work stops, and a later close of the same session
supersedes it. The StopFailure payload names the error in ``error`` (seen on
2.1.168, see tests/fixtures/claude_code); the hooks reference calls it
``error_type``, so both are read. StopFailure and SessionEnd can arrive in
either order; both close the same session, and the last one wins.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import AdapterSpec, CloseEvent, JsonHooksAdapter, PayloadMap

# StopFailure error types that mean "this agent cannot go on now": the ones a
# user switches agents over. Transient API errors (server_error, overloaded)
# usually clear on the next try and do not write a handoff.
LIMIT_ERRORS = ("rate_limit", "billing_error", "account_on_hold", "cloud_credential_error")

SPEC = AdapterSpec(
    name="claude-code",
    display="Claude Code",
    verified=True,
    config_path=lambda home: Path(home) / ".claude" / "settings.json",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(error=("error", "error_type"), compact_events=("PreCompact",)),
    output="text",
    transcript_format="claude-jsonl",
    detect_bins=("claude",),
    detect_dirs=(".claude",),
    config_source="claude",
    hook_timeout=15,
    notes="Verified against Claude Code hook docs and real JSONL transcripts.",
    extra_close_events=(CloseEvent("StopFailure", "|".join(LIMIT_ERRORS)), CloseEvent("PreCompact")),
)


class ClaudeCodeAdapter(JsonHooksAdapter):
    legacy_markers = ("integrations/claude-code/session_start.py",)


ADAPTER = ClaudeCodeAdapter(SPEC)
