"""Every command the crew texts tell an agent to run exists and works.

The crew brief, the YOU and DO NOT TOUCH lines, the gate's refusals and Stop
blocks, the MCP server instructions and tool results, the relay brief's crew
block and the AGENTS.md block all name ``remembra-crew …`` command lines and
Remembra MCP tools. Continuity gap analysis Phase 1 found the crew block telling
agents to run commands that did not exist; this test keeps that from coming back.

It reads the string templates (not docstrings: those document code) of every
module that writes agent-facing text, extracts

* each ``remembra-crew <subcommand> …`` line, with the positional choice (``task
  list``, ``zones push``) and the ``--flags`` it shows, and runs
  ``remembra-crew <subcommand> --help`` through the console-script entry point in
  a subprocess with a temporary HOME: it must exit 0 and list every flag and
  choice the text uses;
* each MCP tool the text tells an agent to call (``call crew_claim``,
  ``crew_task(action="start")``, ``` `crew_status` ```): it must be a tool the
  Remembra MCP server registers, and every keyword argument the text shows must
  be a parameter of that tool, with an allowed value when the parameter is an enum.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
import tomllib
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# Modules that write text an agent reads (templates, refusals, briefs, instructions, tool results).
AGENT_FACING: tuple[str, ...] = (
    "remembra/crew/schemas.py",  # MCP server instructions, MCP tool result templates
    "remembra/crew/gatecore.py",  # YOU / DO NOT TOUCH lines, turn digest, gate refusals, Stop blocks
    "remembra/crew/sessions.py",  # join brief items (restore and adopt commands), inbox titles
    "remembra/crew/claims.py",  # claim refusals
    "remembra/crew/tasks.py",
    "remembra/crew/reports.py",
    "remembra/crew/inbox.py",
    "remembra/crew/channel.py",
    "remembra/crew/checkpoints.py",
    "remembra/mcp/crew.py",  # the MCP crew block and crew_notice
    "remembra/mcp/server.py",  # MCP tool descriptions and results
    "remembra/relay/handoff.py",  # the relay brief's crew block
    "remembra/relay/crew/cli.py",  # the SessionStart crew block, CLI refusals
    "remembra/relay/crew/crewd.py",  # what crewd answers the CLI an agent ran (adopt, report, …)
    "remembra/relay/crew/gate.py",  # hook deny text
    "remembra/relay/adapters/agents_md.py",  # the AGENTS.md crew block
    "remembra/services/relay.py",
)

# "remembra-crew <word>" followed by the rest of the line up to a quote, backtick or newline.
_COMMAND = re.compile(r"remembra-crew[ \t]+([a-z][a-z-]*)(?![a-z-]*:)([^\n`'\"]*)")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
# A tool named after "call"/"use"/"with"/…, or in backticks, or written as a call with its arguments.
_TOOL_MENTION = re.compile(
    r"(?P<pre>\b(?:call|calls|use|using|with|or|via|run|tool)\s+)?(?P<tick>`)?"
    r"\b(?P<name>[a-z]+_[a-z_]+)`?(?P<call>\((?P<args>[^)]*)\))?"
)
_KWARG = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[\w…-]+)")
# Words shaped like tool names that templates use as data, never as a tool to call.
_NOT_TOOLS = frozenset({"request_release", "follow_ups", "not_done", "baton_ref", "session_id", "task_id"})
_PLACEHOLDER = re.compile(r"^(?:X|…|T-[nX0-9]+|<.*>)$")


def _templates(module: str) -> list[tuple[str, str]]:
    """(``module:line``, text) of every string template in ``module``; f-string holes become ``X``."""
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    inside_fstring = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if id(node) in docstrings or id(node) in inside_fstring:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "X" for v in node.values)
        else:
            continue
        if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE)\b", text):
            continue  # SQL, not text for an agent
        out.append((f"{module}:{node.lineno}", text))
    return out


@cache
def _all_templates() -> tuple[tuple[str, str], ...]:
    """The string templates of the agent-facing modules, plus every MCP tool description (what tools/list shows)."""
    from remembra.mcp import server

    described = tuple((f"mcp tool {t.name}", t.description or "") for t in asyncio.run(server.mcp.list_tools()))
    return tuple(t for module in AGENT_FACING for t in _templates(module)) + described


@cache
def command_lines() -> dict[tuple[str, str | None], dict[str, set[str]]]:
    """``(subcommand, positional choice or None) → {"flags": {...}, "where": {...}}``."""
    out: dict[tuple[str, str | None], dict[str, set[str]]] = {}
    for where, text in _all_templates():
        for m in _COMMAND.finditer(text):
            sub, rest = m.group(1), m.group(2)
            rest = rest.split(" # ")[0]
            first = rest.split()[0] if rest.split() else ""
            choice = first if re.fullmatch(r"[a-z][a-z-]*", first) and sub in _POSITIONAL_CHOICES else None
            entry = out.setdefault((sub, choice), {"flags": set(), "where": set()})
            entry["flags"].update(_FLAG.findall(rest))
            entry["where"].add(where)
    return out


@cache
def tool_calls() -> dict[str, dict[str, set[str]]]:
    """``tool → {"args": {"name=value", ...}, "where": {...}}`` for every MCP tool a template names."""
    out: dict[str, dict[str, set[str]]] = {}
    for where, text in _all_templates():
        for m in _TOOL_MENTION.finditer(text):
            name = m.group("name")
            if not (m.group("pre") or m.group("tick") or m.group("call")) or name in _NOT_TOOLS:
                continue
            if not name.startswith("crew_") and name not in _registered_tools():
                continue  # "with dedupe_key", "run inject_text(…)": field and function names, not tools
            entry = out.setdefault(name, {"args": set(), "where": set()})
            entry["where"].add(where)
            for key, value in _KWARG.findall(m.group("args") or ""):
                entry["args"].add(f"{key}={value.strip(chr(34)).strip(chr(39))}")
    return out


# Subcommands whose first positional is a fixed choice the help must list.
_POSITIONAL_CHOICES = frozenset({"task", "zones"})


@cache
def _tool_schemas() -> dict[str, dict[str, object]]:
    """Input schema of every tool the Remembra MCP server registers (what ``tools/list`` returns)."""
    from remembra.mcp import server

    return {t.name: dict(t.inputSchema) for t in asyncio.run(server.mcp.list_tools())}


def _registered_tools() -> frozenset[str]:
    return frozenset(_tool_schemas())


def _help(args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_")},
        "HOME": str(home),
        "PYTHONPATH": str(SRC),
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }
    return subprocess.run(
        [sys.executable, "-c", "from remembra.relay.crew.cli import entrypoint; entrypoint()", *args, "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(home),
        timeout=60,
        check=False,
    )


def test_the_console_script_is_the_cli_entrypoint() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["remembra-crew"] == "remembra.relay.crew.cli:entrypoint"


def test_templates_name_crew_commands_and_tools() -> None:
    # the extraction itself works: the texts agents read do name these (a regex that finds nothing proves nothing)
    subs = {sub for sub, _ in command_lines()}
    assert {"adopt", "report", "checkpoint", "doctor", "say"} <= subs, subs
    assert {"crew_status", "crew_claim", "crew_task", "crew_say", "crew_checkpoint", "crew_report"} <= set(tool_calls())
    assert {"session_brief", "close_session"} <= set(tool_calls())


@pytest.mark.parametrize(
    "command", sorted(command_lines(), key=lambda c: (c[0], c[1] or "")), ids=lambda c: " ".join(filter(None, c))
)
def test_every_command_a_template_shows_exists(command: tuple[str, str | None], tmp_path: Path) -> None:
    sub, choice = command
    entry = command_lines()[command]
    res = _help([sub], tmp_path)
    assert res.returncode == 0, (sub, sorted(entry["where"]), res.stdout, res.stderr)
    shown = res.stdout + res.stderr
    assert "usage:" in shown, (sub, shown)
    if choice is not None:
        assert re.search(rf"\b{re.escape(choice)}\b", shown), (sub, choice, sorted(entry["where"]), shown)
    missing = sorted(f for f in entry["flags"] if f not in shown)
    assert missing == [], (sub, missing, sorted(entry["where"]), shown)


@pytest.mark.parametrize("tool", sorted(tool_calls()))
def test_every_mcp_tool_a_template_names_is_registered(tool: str) -> None:
    from remembra.crew.schemas import MCP_TOOLS

    entry = tool_calls()[tool]
    assert tool in _registered_tools(), (tool, sorted(entry["where"]))
    properties = _tool_schemas()[tool].get("properties") or {}
    assert isinstance(properties, dict)
    spec = next((t for t in MCP_TOOLS if t.name == tool), None)
    enums = {p.name: p.enum for p in spec.params} if spec is not None else {}
    for arg in sorted(entry["args"]):
        key, value = arg.split("=", 1)
        assert key in properties, (tool, key, sorted(properties), sorted(entry["where"]))
        enum = enums.get(key)
        if enum and not _PLACEHOLDER.match(value):
            assert value in enum, (tool, key, value, enum, sorted(entry["where"]))


def test_agent_texts_never_name_one_customer() -> None:
    """Templates went to every account with the first customer's name in them ("Mani paused this session")."""
    from remembra.mcp import server

    named = [(where, text) for where, text in _all_templates() if re.search(r"\bMani\b", text)]
    assert named == []
    tools = asyncio.run(server.mcp.list_tools())
    assert [t.name for t in tools if re.search(r"\bMani\b", t.description or "")] == []
    assert not re.search(r"\bMani\b", server.CREW_MCP_INSTRUCTIONS)


def test_a_missing_command_flag_or_tool_is_caught(tmp_path: Path) -> None:
    """The checks above fail for a command, flag or tool that does not exist (they are not vacuous)."""
    assert _help(["frobnicate"], tmp_path).returncode != 0
    assert "--no-such-flag" not in _help(["adopt"], tmp_path).stdout
    assert "crew_frobnicate" not in _registered_tools()
    found = {m.group(1) for m in _COMMAND.finditer('run: remembra-crew frobnicate --x "…"')}
    assert found == {"frobnicate"}
    assert {m.group("name") for m in _TOOL_MENTION.finditer('call crew_frobnicate(action="x")')} == {"crew_frobnicate"}


def test_the_top_level_help_lists_the_public_commands_only() -> None:
    """``bypass`` is the owner's, run at their own terminal (D34): it works but is not advertised, and the help
    never prints argparse's ``==SUPPRESS==`` marker."""
    from remembra.relay.crew import cli

    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "==SUPPRESS==" not in help_text
    assert "bypass" not in help_text
    assert "connect" in help_text and "verify" in help_text and "{start," in parser.format_usage()
    args = parser.parse_args(["bypass", "--session", "cc-1"])
    assert args.command == "bypass" and args.session == "cc-1" and args.minutes == 15
