"""What happens to the arguments of an MCP tool call before the tool runs.

Agents guess argument names: most hosts defer tool schemas, so a call is often written from the
tool's name alone. FastMCP's argument model ignores a key it does not know, so a guessed name used
to vanish: ``crew_say(message=...)`` failed as "body required", ``close_session(open_work=[...])``
dropped the open work, and ``crew_claim(project="x")`` ran in whatever project the session was on.

``prepare`` makes every key count. A key is one of:

* declared by the tool: passed through;
* a known other name for a declared argument (``ALIASES``, and ``project`` for ``project_id``):
  moved to that argument;
* a project or location on a crew tool that has no such argument (``CREW_SCOPED``; a crew tool
  declares ``project_id`` and ``root_path``, so in practice ``git_remote``): handed back in
  ``Prepared.scope`` for the server to check against the crew the call acts on;
* anything else: the call is refused in one line that names the key, with what to use instead
  (``HINTS``) or the declared argument it resembles. A value is never echoed.

A key whose value is null asks for nothing and is dropped. The tables here are data only; the MCP
server (``remembra.mcp.server``) installs the guard and does the crew check.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

PREFIX: Final = "INVALID ARGUMENTS: "
NOTHING_DONE: Final = "Nothing was done."

# tool -> {other name: declared argument}
ALIASES: Final[Mapping[str, Mapping[str, str]]] = {
    "crew_say": {"message": "body"},
    "send_to_inbox": {"to": "to_agent"},
    "remembra_help": {"topic": "question", "query": "question"},
    "ack_inbox": {"id": "inbox_id"},
    "close_session": {"open_work": "todos_open"},
    "crew_claim": {"note": "reason", "intent": "reason"},
}
# The other names an MCP client is told about: hosts that check `required` themselves never forward
# a call without the canonical name unless the schema says the other one is accepted.
ADVERTISED: Final[Mapping[str, tuple[str, ...]]] = {
    "crew_say": ("message",),
    "send_to_inbox": ("to",),
    "remembra_help": ("topic",),
    "close_session": ("open_work",),
}
SCOPE_KEYS: Final = ("project", "project_id", "root_path", "git_remote")
# Crew tools act on the crew this session joined, or on the project a call names with the arguments the
# tool declares (project_id, root_path). A scope key such a tool does not declare is checked, not dropped.
CREW_SCOPED: Final = frozenset({"crew_claim", "crew_guard", "crew_task", "crew_say", "crew_checkpoint", "crew_report"})
# send_to_inbox tags a message with a project through metadata.project_id.
_METADATA_PROJECT: Final = frozenset({"send_to_inbox"})

_PER_AGENT: Final = "The inbox is per agent, not per project: leave it out."
_SESSION_PROJECT: Final = (
    "This tool acts on the project your session_brief resolved (session_brief(root_path=...) or "
    "session_brief(project_id=...)), else the configured one: leave it out."
)
# (tool, key) -> what to do instead. "*" is every tool that has no entry of its own.
HINTS: Final[Mapping[tuple[str, str], str]] = {
    ("crew_claim", "claim_ids"): (
        'Release by what the claim covers: zone="pos", paths=[...] or task="T-14" (crew_status lists what you hold).'
    ),
    ("crew_claim", "zones"): 'Pass one zone="pos" per call, or paths=[...], or task="T-14" for all of a task\'s zones.',
    ("crew_checkpoint", "trigger"): (
        "The trigger is derived: commit when you pass commits, test when you pass tests, otherwise turn. "
        "Say what happened in summary."
    ),
    ("crew_checkpoint", "note"): "Use summary.",
    ("crew_checkpoint", "notes"): "Use summary.",
    ("crew_checkpoint", "message"): "Use summary.",
    ("crew_checkpoint", "tests_passing"): "Use tests=[{command, passed, failed}] (counts).",
    ("crew_task", "message"): "Use note.",
    ("crew_report", "files_changed"): "Files are taken from your checkpoints: call crew_checkpoint(files_changed=[...]) first.",
    ("get_inbox", "project"): _PER_AGENT,
    ("get_inbox", "project_id"): _PER_AGENT,
    ("ack_inbox", "project"): _PER_AGENT,
    ("ack_inbox", "project_id"): _PER_AGENT,
    ("*", "project"): _SESSION_PROJECT,
    ("*", "project_id"): _SESSION_PROJECT,
    ("*", "root_path"): _SESSION_PROJECT,
    ("*", "git_remote"): _SESSION_PROJECT,
}


@dataclass
class Prepared:
    """The arguments to run the tool with, or why not."""

    arguments: dict[str, Any]
    # project / location keys of a crew tool, to be checked by the server before the tool runs
    scope: dict[str, str] = field(default_factory=dict)
    # declared argument -> the name the caller used for it
    sent_as: dict[str, str] = field(default_factory=dict)
    refusal: str | None = None


def takes(tool: str, declared: Sequence[str]) -> str:
    return f"{tool} takes: {', '.join(declared)}." if declared else f"{tool} takes no arguments."


class InvalidArguments(Exception):
    """Raised inside a tool whose arguments fit the schema and still cannot work (its text is a ``refusal``).

    The MCP server answers it like every other argument failure: an error result carrying that text.
    """


def refusal(tool: str, declared: Sequence[str], problems: Sequence[str]) -> str:
    reasons = "; ".join(problems[:5])
    if reasons[-1:] not in (".", "!", "?"):
        reasons += "."
    return f"{PREFIX}{reasons} {takes(tool, declared)} {NOTHING_DONE}"


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _key(name: Any) -> str:
    """A caller's key, safe to show: printable ASCII, clipped."""
    text = "".join(c if c.isascii() and (c.isalnum() or c in "_-.") else "?" for c in str(name))
    return text[:40] or "?"


def aliases_for(tool: str, declared: Sequence[str]) -> dict[str, str]:
    """Other name -> declared argument, for one tool."""
    out = {a: c for a, c in ALIASES.get(tool, {}).items() if c in declared and a not in declared}
    if "project_id" in declared and "project" not in declared:
        out["project"] = "project_id"
    return out


def prepare(
    tool: str,
    declared: Sequence[str],
    arguments: Any,
    *,
    normalize_project: Callable[[str], str] = str,
) -> Prepared:
    """Sort a call's keys into declared arguments, aliases, crew scope and refusals (see the module text)."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        return Prepared(
            {}, refusal=f"{PREFIX}{tool} takes named arguments (a JSON object). {takes(tool, declared)} {NOTHING_DONE}"
        )
    args: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for name, value in arguments.items():
        if name in declared:
            args[str(name)] = value
        elif value is not None:
            extras[str(name)] = value
    out = Prepared(args)
    problems: list[str] = []

    for alias, canonical in aliases_for(tool, declared).items():
        if alias not in extras:
            continue
        value = extras.pop(alias)
        mine = args.get(canonical)
        if _blank(mine) and (mine is None or not _blank(value)):
            # nothing under the declared name (or an empty string next to a real value): the alias is the argument
            args[canonical] = value
            out.sent_as[canonical] = alias
        elif _blank(value) and isinstance(value, str):
            continue  # an empty alias next to a value says nothing
        elif not _same(mine, value, canonical, normalize_project):
            problems.append(f"{canonical} and {alias} are the same argument and differ: pass one")

    if tool in _METADATA_PROJECT:
        named = [k for k in ("project_id", "project") if k in extras]
        values = {normalize_project(str(extras.pop(k))) for k in named}
        metadata = args.get("metadata")
        if len(values) > 1:
            problems.append("project and project_id are the same argument and differ: pass one")
        elif values and isinstance(metadata, Mapping) and metadata.get("project_id"):
            if normalize_project(str(metadata["project_id"])) not in values:
                problems.append(f"{named[0]} and metadata.project_id differ: pass one")
        elif values and (metadata is None or isinstance(metadata, Mapping)):
            args["metadata"] = {**(metadata or {}), "project_id": values.pop()}
        elif values:
            problems.append(f"{named[0]} needs metadata to be an object")

    if tool in CREW_SCOPED:
        for name in SCOPE_KEYS:
            if name in extras and name not in declared:
                value = extras.pop(name)
                if isinstance(value, str) and value.strip():
                    out.scope[name] = value.strip()
                else:
                    problems.append(f"{name}: expected a non-empty string")

    for name in extras:
        hint = HINTS.get((tool, name)) or HINTS.get(("*", name))
        if not hint:
            close = difflib.get_close_matches(name, [*declared, *aliases_for(tool, declared)], n=1, cutoff=0.6)
            hint = f"Did you mean {close[0]}?" if close else None
        problems.append(f"{tool} does not take {_key(name)}." + (f" {hint}" if hint else ""))

    if problems:
        out.refusal = refusal(tool, declared, problems)
    return out


def _same(mine: Any, other: Any, canonical: str, normalize_project: Callable[[str], str]) -> bool:
    if canonical == "project_id" and isinstance(mine, str) and isinstance(other, str):
        return normalize_project(mine) == normalize_project(other)
    return bool(mine == other)


def advertise(tool: str, schema: dict[str, Any]) -> None:
    """Make a tool's published inputSchema say what ``prepare`` does: no undeclared keys, and the
    other names a client may use in place of a required argument (which then is not ``required``)."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = schema["properties"] = {}
    declared = list(properties)
    known = aliases_for(tool, declared)
    for alias in ADVERTISED.get(tool, ()):
        canonical = known.get(alias)
        if canonical is None:
            continue
        shown = {k: v for k, v in dict(properties[canonical]).items() if k not in ("title", "default", "description")}
        if "anyOf" not in shown and shown.get("type") != "null":
            shown = {"anyOf": [shown, {"type": "null"}]}
        properties[alias] = {
            **shown,
            "default": None,
            "title": alias.replace("_", " ").title(),
            "description": f"Same as {canonical}.",
        }
        required = schema.get("required")
        if isinstance(required, list) and canonical in required:
            required.remove(canonical)
            properties[canonical] = {**properties[canonical], "description": f"Required ({alias} is accepted in its place)."}
    schema["additionalProperties"] = False


def validation_text(
    tool: str, declared: Sequence[str], sent: Sequence[str], errors: Sequence[Mapping[str, Any]], sent_as: Mapping[str, str]
) -> str:
    """A pydantic argument failure as one line: which argument, what is wrong, what the tool takes.

    Nothing of the caller's input is repeated (pydantic's own text echoes it and links to its docs).
    """
    problems: list[str] = []
    for err in errors:
        loc = [
            str(part)
            for part in (err.get("loc") or ())
            if not str(part).startswith(("function-", "list[", "dict[", "str", "int"))
        ]
        name = ".".join(loc[:3]) or "arguments"
        alias = sent_as.get(loc[0]) if loc else None
        label = f"{name} (sent as {alias})" if alias else name
        if err.get("type") == "missing":
            problem = f"{label}: required"
        else:
            message = str(err.get("msg") or "invalid").replace("Input should be", "expected").rstrip(".")
            problem = f"{label}: {message[:120]}"
        if problem not in problems:
            problems.append(problem)
    line = f"{PREFIX}{'; '.join(problems[:5]) or 'the arguments do not fit this tool'}. {takes(tool, declared)}"
    return f"{line} You sent: {', '.join(sorted(_key(k) for k in sent)) or 'nothing'}."
