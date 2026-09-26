"""What Marshal says, word for word, wherever it says it.

The doctor (``remembra-relay doctor``, the MCP tool ``remembra_doctor``) and
the dashboard's "why?" slips diagnose some of the same faults from different
evidence: the doctor reads this machine and the trail, a slip reads your keys
and the trail. When both see the same fault they give it the same rule id,
the same sentence and the same page. Those sentences live here.

The dashboard is built without this package, so
``scripts/sync_marshal_words.py`` writes them into
``dashboard/src/lib/marshalWords.ts`` and fails in ``--check`` mode when that
copy is stale. ``tests/test_marshal_parity.py`` runs the doctor on the
dashboard's own verdict fixture and holds both to the same words.

Placeholders are ``{name}``-style; :func:`say` fills them here and ``say()``
in ``lib/marshal.ts`` fills them in the dashboard. A missing value is an
error on both sides, never an empty gap.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from remembra.relay.adapters import REGISTRY

# The names the dashboard shows (``KNOWN`` in dashboard/src/lib/agents.ts): the doctor uses the same ones.
AGENT_NAMES: dict[str, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "qwen": "Qwen Code",
    "kimi": "Kimi CLI",
}

# Other spellings of the same agents (``ALIASES`` in dashboard/src/lib/agents.ts): a pickup recorded as
# "claude" or "codex-cli" counts for Claude Code or Codex on both surfaces.
AGENT_ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claude code": "claude-code",
    "claude_code": "claude-code",
    "codex-cli": "codex",
    "openai-codex": "codex",
    "gemini-cli": "gemini",
    "qwen-code": "qwen",
    "kimi-cli": "kimi",
}

# The rules both surfaces can reach. Each part is a template:
#   what    the call (the slip's "=" line; the doctor's finding, followed by `detail` when there is one)
#   detail  one more sentence of why
#   fix     the one action, when both surfaces give the same one
#   doc     the anchor on docs.remembra.dev/guides/relay/ ("" is the top of the guide)
SHARED_RULES: dict[str, dict[str, str]] = {
    "KEY_MISSING": {
        "what": "No API key {where}, so the hooks can't load or save handoffs.",
        "doc": "#setup",
    },
    "PICKS_UP_NEVER_CLOSES": {
        "what": "{name} read {briefs} but never handed off: its close hasn't reached Remembra.",
        "fix": "End one {name} session. If no handoff arrives, doctor names the failing close:",
        "doc": "#doctor",
    },
    "CODEX_TRUST_MISSING": {
        "what": "Codex needs you to trust {hooks}: Codex Settings > Hooks > Trust.",
        "detail": "Codex skips untrusted hooks without a message.",
        "fix": "Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust {events}.",
        "doc": "#codex-trust",
    },
    "NOTHING_WAITING": {
        "what": "{name} hasn't ended a session with the hooks yet, and no handoff was waiting for it.",
        "fix": "End one {name} session in a repository.",
        "doc": "#doctor",
    },
    "HOOKS_NOT_FIRING": {
        "what": "The key works, but nothing from {name} has reached Remembra{since}.",
        "doc": "#doctor",
    },
    "STALE_CHECKPOINT": {
        "what": "{name}'s last session stopped without a handoff.",
        "detail": "Its newest entry is a checkpoint from {ago}, with no handoff after it.",
        "fix": "In the repository it worked in, write the handoff now:",
        "doc": "",
    },
}

# Verdicts only a slip reaches: it sees whether any key was ever used, and says when nothing is wrong.
DASHBOARD_ONLY: tuple[str, ...] = ("KEY_NEVER_USED", "HANDED_OFF")

# Said of every adapter that has never been run against its tool.
UNVERIFIED = "{name}'s adapter is built from its hook docs and has never been run against the real tool."

# The hooks Codex must trust: the events connect writes for it, in order.
CODEX_TRUST_EVENTS: tuple[str, ...] = tuple(dict.fromkeys(event for _, event in REGISTRY["codex"].events()))


def agent_name(agent: str) -> str:
    """The agent's name as the dashboard shows it; an adapter's own display name for one it doesn't know."""
    if agent in AGENT_NAMES:
        return AGENT_NAMES[agent]
    adapter = REGISTRY.get(agent)
    return adapter.spec.display if adapter is not None else agent


def canonical_agent(label: str) -> str:
    """``label`` lower-cased, with a known other spelling mapped to the agent's id (``canonicalAgentId``)."""
    key = label.strip().lower()
    return AGENT_ALIASES.get(key, key)


def join_names(names: Iterable[str]) -> str:
    """Names as a sentence lists them: "A", "A and B", "A, B and C"."""
    items = list(names)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def say(rule: str, part: str, **values: Any) -> str:
    """One part of a shared rule, filled in. Raises ``KeyError`` for a missing part or value."""
    return SHARED_RULES[rule][part].format(**values)


def codex_trust_fix(events: Iterable[str] = CODEX_TRUST_EVENTS) -> str:
    """The one Codex trust step, naming ``events`` (all three unless only some are untrusted)."""
    return say("CODEX_TRUST_MISSING", "fix", events=join_names(dict.fromkeys(events)))


def codex_trust_call(n: int = len(CODEX_TRUST_EVENTS)) -> str:
    return say("CODEX_TRUST_MISSING", "what", hooks=plural(n, "hook"))


def unverified_line(agent: str) -> str:
    return UNVERIFIED.format(name=agent_name(agent))
