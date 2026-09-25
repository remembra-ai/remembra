"""Fallback for agents without hooks: an idempotent, marked AGENTS.md section.

Most coding agents read AGENTS.md; together with the MCP server instructions
and the ``close_session`` tool this covers agents with no lifecycle hooks.
"""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters.base import Change

BEGIN = "<!-- remembra-relay:start -->"
END = "<!-- remembra-relay:end -->"


def block(relay: str) -> str:
    return "\n".join(
        [
            BEGIN,
            "## Session continuity (Remembra Relay)",
            "",
            f"- At session start run `{relay} brief --agent <your-agent-id>` (or call the `session_brief` MCP tool)"
            ' and continue from its "Last session" line.',
            f"- Before you finish run `{relay} close --agent <your-agent-id>` (or call the `close_session` MCP tool)"
            " with what is done, not done, failing and the next step.",
            END,
            "",
        ]
    )


def plan(path: Path, relay: str) -> Change:
    before = path.read_text(encoding="utf-8") if path.exists() else None
    text = before or ""
    section = block(relay)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        after = head + section + tail.lstrip("\n")
    else:
        after = (text.rstrip() + "\n\n" if text.strip() else "") + section
    summary = [] if after == text else [f"write the Remembra Relay section in {path}"]
    return Change(path=path, before=before, after=after, summary=summary)
