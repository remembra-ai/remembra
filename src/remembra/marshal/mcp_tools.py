"""Marshal's MCP tools and prompt, registered on the Remembra MCP server.

``remembra_doctor``, ``remembra_setup`` and ``remembra_help`` are read-only
(they never write, never run a fix, never return a key). On a remote
transport the first two answer ``local_only``: they read this machine's
files, which a hosted server does not have. ``remembra_help`` reads no local
file and works everywhere.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from remembra.marshal import tools

INSTRUCTIONS_SENTENCE = (
    " If Remembra itself misbehaves (no brief, handoffs not arriving, a rejected key), call remembra_doctor and show"
    " its rendered slip; never run a fix without the user's yes."
)

DOCTOR_DESCRIPTION = """Say why Remembra handoffs or briefs aren't arriving on this machine, from evidence. Reads only.

Reads the relay config, the unsent-handoff queue, each agent's hook file, Codex's hook trust and (with
check_server) at most four GETs of the user's own trail. It never writes, never runs a fix, and never
returns a key.

Show `rendered` verbatim in a code block. Offer fixes one at a time, naming what each writes, and run one
only after the user says yes to that fix. A fix with `runs_where: user_terminal` involves the API key: ask
the user to run it in their own terminal. `codex_ui` and `dashboard` fixes are done by the user there.
`[!!]` is proven from what was read; `[??]` is inferred.

Args:
    agent: Only this agent (claude-code, codex, cursor, gemini, qwen, kimi). Default: every agent.
    check_server: Also read the user's trail (GET only). False reads this machine only.
"""

SETUP_DESCRIPTION = """The exact steps to install Remembra Relay on this machine for its OS and agents. Reads only.

Each step has a command (from Remembra's fixed templates) or an action, where it runs, what it writes,
and whether it is already done here. Show `rendered`, then walk the steps one at a time, asking before
each one that installs or writes anything. The key step is always the user's: they create the key at
app.remembra.dev and type it into remembra-install's hidden prompt in their own terminal. Never ask for
a key in chat and never put one on a command line.

Args:
    agents: Agents to set up (claude-code, codex, cursor, gemini, qwen, kimi). Default: those found here.
"""

HELP_DESCRIPTION = """Answer a question about Remembra from its bundled docs, quoted, or say it can't confirm.

The answer is the docs' own text with its page URL (the relay guide and the plans page), plus facts
from the code (plan limits, crew mode, Windows). Refunds and cancelling, compliance, security, privacy
(who sees the data, selling or sharing it), hosting and data location, retention, deleting account data,
subprocessors, training and uptime are never answered here: read the page it returns, quote it, or say
you can't confirm. Uninstalling is not deleting account data. Show `rendered`; don't add prices, dates or
promises that aren't in it.

Args:
    question: The user's question, in their words.
"""


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


def register(mcp: FastMCP, is_remote: Callable[[], bool]) -> None:
    """Add the three tools and the ``doctor`` prompt to ``mcp``, and one line to its instructions."""
    server = mcp._mcp_server
    if INSTRUCTIONS_SENTENCE.strip() not in (server.instructions or ""):
        server.instructions = (server.instructions or "") + INSTRUCTIONS_SENTENCE

    read_only = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}

    @mcp.tool(
        name="remembra_doctor",
        description=DOCTOR_DESCRIPTION,
        annotations=ToolAnnotations(title="Remembra Doctor", openWorldHint=True, **read_only),
    )
    def remembra_doctor(agent: str | None = None, check_server: bool = True) -> str:
        if is_remote():
            return _dump(tools.LOCAL_ONLY)
        try:
            return _dump(tools.doctor_payload(agent, check_server))
        except Exception as e:  # a diagnosis tool that raises tells the user nothing
            return _dump({"status": "error", "error": f"doctor failed: {e.__class__.__name__}", "changed_nothing": True})

    @mcp.tool(
        name="remembra_setup",
        description=SETUP_DESCRIPTION,
        annotations=ToolAnnotations(title="Remembra Setup", openWorldHint=False, **read_only),
    )
    def remembra_setup(agents: list[str] | None = None) -> str:
        if is_remote():
            return _dump(tools.LOCAL_ONLY)
        try:
            return _dump(tools.setup_payload(agents))
        except Exception as e:
            return _dump({"status": "error", "error": f"setup failed: {e.__class__.__name__}", "changed_nothing": True})

    @mcp.tool(
        name="remembra_help",
        description=HELP_DESCRIPTION,
        annotations=ToolAnnotations(title="Remembra Help", openWorldHint=False, **read_only),
    )
    def remembra_help(question: str) -> str:
        return _dump(tools.help_payload(question))

    @mcp.prompt(
        name="doctor",
        title="Remembra Doctor",
        description="Find where the baton dropped between your agents, and fix it one step at a time.",
    )
    def doctor_prompt(agent: str | None = None) -> list[dict[str, str]]:
        target = f" for {agent}" if agent else ""
        return [
            {
                "role": "user",
                "content": (
                    f"Call the remembra_doctor tool{target} and show its `rendered` slip verbatim in a code block. "
                    "Then offer the fixes one at a time, saying what each one writes, and run a fix only after I say "
                    "yes to it. If a fix runs in my own terminal (it involves my API key), give me the command instead "
                    "of running it. Never ask me for my key."
                ),
            }
        ]
