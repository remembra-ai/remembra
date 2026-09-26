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
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

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
    notes="Verified against Claude Code hook docs and real JSONL transcripts.",
    extra_close_events=(CloseEvent("StopFailure", "|".join(LIMIT_ERRORS)), CloseEvent("PreCompact")),
)


class ClaudeCodeAdapter(JsonHooksAdapter):
    legacy_markers = ("integrations/claude-code/session_start.py",)


ADAPTER = ClaudeCodeAdapter(SPEC)


# ---------------------------------------------------------------------------
# Crew mode (§8.2): the full hook set, written by `remembra-crew connect`
# ---------------------------------------------------------------------------

# File tools, Bash and every MCP tool (MCP write tools are gated through the tool map, §8.2).
CREW_TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*"

CREW = CrewSpec(
    adapter="claude-code",
    verified=True,
    style="json-hooks",
    hooks=(
        CrewHook("SessionStart", "start", "cli", timeout=15),
        CrewHook("UserPromptSubmit", "turn", "gate", timeout=3),
        CrewHook("PreToolUse", "pretool", "gate", matcher=CREW_TOOL_MATCHER, timeout=5),
        CrewHook("PostToolUse", "posttool", "gate", matcher=CREW_TOOL_MATCHER, timeout=30, is_async=True),
        CrewHook("Stop", "stop", "gate", timeout=5),
        # S0 (docs/crew/S0-results.md): asyncRewake is L0. The waiter wakes an idle session for a granted
        # queued claim, a human hand-over or a pause; it must exit 0 at once when nothing is queued.
        CrewHook("Stop", "rewake", "gate", timeout=300, async_rewake=True),
        CrewHook("StopFailure", "stall", "cli", timeout=20),
        CrewHook("PreCompact", "precompact", "gate", timeout=10),
        CrewHook("SessionEnd", "end", "cli", timeout=20),
    ),
    output="hook-json",
    pretool_events=("PreToolUse",),
    file_gate=True,
    project_config=lambda repo: Path(repo) / ".claude" / "settings.json",
    notes="Verified: hook schema read from Claude Code 2.1.168 and proven live in spike S0.",
)
CREW_LEGACY_MARKERS = ClaudeCodeAdapter.legacy_markers
