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

``CLAUDE_CONFIG_DIR`` moves ``~/.claude`` (settings and transcripts): the
recorded payloads above came from a run with it set.

Other agents run these hooks too: Grok Build loads ``~/.claude/settings.json``
by default and ``/import-claude`` copies it; Cursor (IDE and cursor-agent)
imports it and runs SessionStart / SessionEnd / PreCompact with its own
payload, in ``~/.claude``; Devin reads it while ``read_config_from.claude`` is
on; Continue's ``cn`` reads it; ``gemini hooks migrate`` and ``kimi migrate``
copy it with ``--agent claude-code`` in the command. The relay tells those
runs apart (:mod:`remembra.relay.hosts`): the brief does nothing, and the close
is filed under the agent that ran it, or dropped when the relay has no adapter
for that agent. A payload whose transcript is under this agent's own directory
is always Claude Code's.
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
    hook_timeouts={"start": 15, "end": 15},
    home_env="CLAUDE_CONFIG_DIR",
    notes="Verified against Claude Code hook docs and real JSONL transcripts.",
    extra_close_events=(CloseEvent("StopFailure", "|".join(LIMIT_ERRORS)), CloseEvent("PreCompact")),
)


class ClaudeCodeAdapter(JsonHooksAdapter):
    legacy_markers = ("integrations/claude-code/session_start.py",)


ADAPTER = ClaudeCodeAdapter(SPEC)
