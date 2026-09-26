"""Crew-mode hook entries in agent config files (spec §8.2, §8.3; WP-10).

Each adapter module declares a :class:`CrewSpec`: which agent events run which
crew verb, with which matcher and timeout. The renderers here turn the agent's
current config text into the new text, idempotently:

* every crew entry carries :data:`CREW_MARKER` (``# remembra-crew``) at the end
  of its command string, so re-running replaces exactly our entries and the
  gate's tamper protection (``gatecore.crew_hook_entries``, D28) can find them;
* the existing ``remembra-relay`` SessionStart/SessionEnd entries and the legacy
  ``integrations/claude-code/session_start.py`` hook are removed, because
  ``remembra-crew start`` / ``end`` run the relay brief and close themselves;
* everything else in the file is kept, and an untouched file keeps its exact
  formatting.

Commands have two runners (the interface to WP-9's runtime):

* ``gate``: ``<python> -I <home>/.remembra/crew/bin/crew-gate.py <verb> --hook <adapter> # remembra-crew``
  (verbs ``turn``, ``pretool``, ``posttool``, ``stop``, ``precompact`` and, per
  S0, ``rewake`` for the asyncRewake waiter);
* ``cli``: ``<remembra-crew> <verb> --hook <adapter> --agent <adapter> # remembra-crew``
  (verbs ``start``, ``stall``, ``end``).

Hook commands never carry secrets: S0 showed Claude Code prints the full hook
command line to the model in asyncRewake messages.
"""

from __future__ import annotations

import copy
import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from remembra.crew.schemas import CREW_HOOK_MARKER
from remembra.relay.adapters.base import _load_json_object, is_relay_command

CREW_MARKER = CREW_HOOK_MARKER
GATE_VERBS = ("turn", "pretool", "posttool", "stop", "precompact", "rewake")
CLI_VERBS = ("start", "stall", "end")
KIMI_BEGIN = "# >>> remembra-crew (managed block) >>>"
KIMI_END = "# <<< remembra-crew (managed block) <<<"
KIMI_RELAY_BEGIN = "# >>> remembra-relay (managed block) >>>"
KIMI_RELAY_END = "# <<< remembra-relay (managed block) <<<"


@dataclass(frozen=True)
class CrewHook:
    """One agent event wired to one crew verb."""

    event: str
    verb: str
    runner: Literal["gate", "cli"]
    matcher: str | None = None
    timeout: int | None = None
    is_async: bool = False
    async_rewake: bool = False

    def __post_init__(self) -> None:
        allowed = GATE_VERBS if self.runner == "gate" else CLI_VERBS
        if self.verb not in allowed:
            raise ValueError(f"unknown {self.runner} verb {self.verb!r}")


@dataclass(frozen=True)
class ToolFields:
    """Where a pre/post-tool payload carries the tool name, its input, a file path and a shell command.

    Keys are looked up in the payload first, then in the tool-input object.
    Used by ``remembra-crew verify`` to check captured payloads (§8.3 rule 2).
    """

    tool_name: tuple[str, ...] = ("tool_name",)
    tool_input: tuple[str, ...] = ("tool_input",)
    file_path: tuple[str, ...] = ("file_path", "notebook_path", "path", "absolute_path")
    command: tuple[str, ...] = ("command",)

    def extract(self, payload: Mapping[str, Any]) -> dict[str, str | None]:
        tool_input: Mapping[str, Any] = {}
        for key in self.tool_input:
            value = payload.get(key)
            if isinstance(value, Mapping):
                tool_input = value
                break

        def first(keys: tuple[str, ...]) -> str | None:
            for source in (payload, tool_input):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return None

        return {"tool_name": first(self.tool_name), "file_path": first(self.file_path), "command": first(self.command)}


@dataclass(frozen=True)
class CrewSpec:
    """How crew mode is wired into one agent (§8.2 for Claude Code, §8.3 for the others)."""

    adapter: str
    verified: bool  # the hook schema was verified against the installed tool (Claude Code only)
    style: Literal["json-hooks", "cursor", "kimi-toml"]
    hooks: tuple[CrewHook, ...]
    output: str  # gate output mode for this agent: hook-json | cursor-json | text
    pretool_events: tuple[str, ...]  # events whose payload is a pre-write/pre-shell decision point
    file_gate: bool  # the agent has a pre-write hook on its file tools (Cursor does not)
    tools: ToolFields = field(default_factory=ToolFields)
    project_config: Callable[[Path], Path] | None = None  # repo -> project-level config file, if supported
    notes: str = ""


@dataclass(frozen=True)
class CrewCommands:
    """Absolute commands the hooks run (hooks start with a minimal PATH)."""

    python: str  # interpreter for the stdlib-only gate
    gate: str  # <home>/.remembra/crew/bin/crew-gate.py
    crew: str  # remembra-crew command, already shell-quoted (may be "<python> -m remembra.relay.crew.cli")

    def command(self, adapter: str, hook: CrewHook) -> str:
        if hook.runner == "gate":
            base = f"{shlex.quote(self.python)} -I {shlex.quote(self.gate)} {hook.verb} --hook {adapter}"
        else:
            base = f"{self.crew} {hook.verb} --hook {adapter} --agent {adapter}"
        return f"{base} {CREW_MARKER}"


def is_crew_command(command: Any) -> bool:
    return isinstance(command, str) and CREW_MARKER in command


# ---------------------------------------------------------------------------
# Claude-style JSON hooks: {"hooks": {"<Event>": [{"matcher"?, "hooks": [{"type": "command", ...}]}]}}
# ---------------------------------------------------------------------------


def json_hook_entry(spec: CrewSpec, hook: CrewHook, cmds: CrewCommands) -> dict[str, Any]:
    inner: dict[str, Any] = {"type": "command"}
    if hook.is_async:
        inner["async"] = True
    if hook.async_rewake:
        inner["asyncRewake"] = True
    if hook.timeout is not None:
        inner["timeout"] = hook.timeout
    inner["command"] = cmds.command(spec.adapter, hook)
    group: dict[str, Any] = {}
    if hook.matcher is not None:
        group["matcher"] = hook.matcher
    group["hooks"] = [inner]
    return group


def _strip_group(group: Any, legacy: tuple[str, ...], strip_relay: bool) -> tuple[Any | None, list[str]]:
    """Remove crew (and relay/legacy) hooks from one group; ``None`` when nothing of the group is left."""
    inner = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(inner, list):
        return group, []
    removed: list[str] = []
    kept: list[Any] = []
    for hook in inner:
        command = hook.get("command") if isinstance(hook, dict) else None
        legacy_hit = isinstance(command, str) and any(m in command for m in legacy)
        if is_crew_command(command) or (strip_relay and (is_relay_command(command) or legacy_hit)):
            removed.append(str(command))
        else:
            kept.append(hook)
    if not removed:
        return group, []
    if not kept:
        return None, removed
    return {**group, "hooks": kept}, removed


def render_json_hooks(
    before: str | None,
    spec: CrewSpec,
    cmds: CrewCommands,
    *,
    remove: bool = False,
    legacy_markers: tuple[str, ...] = (),
    path_hint: str = "config",
) -> tuple[str, list[str]]:
    """New config text and a human summary. ``remove`` takes the crew entries out (uninstall)."""
    data = _load_json_object(before, path_hint)
    new: dict[str, Any] = copy.deepcopy(data)
    hooks = new.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError(f"'hooks' in {path_hint} is not an object")
    wanted: dict[str, list[dict[str, Any]]] = {}
    if not remove:
        for hook in spec.hooks:
            wanted.setdefault(hook.event, []).append(json_hook_entry(spec, hook, cmds))

    summary: list[str] = []
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        ours = [g for g in groups if _is_pure_crew_group(g)]
        others_clean = all(_strip_group(g, legacy_markers, not remove)[1] == [] for g in groups if g not in ours)
        if not remove and ours == wanted.get(event) and others_clean:
            wanted.pop(event, None)  # already exactly right: keep positions
            continue
        kept: list[Any] = []
        for group in groups:
            stripped, removed = _strip_group(group, legacy_markers, not remove)
            for command in removed:
                if is_crew_command(command):
                    summary.append(f"{event}: {'remove' if remove else 'replace'} crew hook `{command}`")
                else:
                    summary.append(f"{event}: remove `{command}` (crew start/end run the relay brief and close)")
            if stripped is not None:
                kept.append(stripped)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    for event, groups in wanted.items():
        hooks.setdefault(event, [])
        for group in groups:
            hooks[event].append(group)
            summary.append(f"{event}: add `{group['hooks'][0]['command']}`")
    if hooks:
        new["hooks"] = hooks
    else:
        new.pop("hooks", None)
    summary = _dedupe(summary)
    if new == data and before is not None:
        return before, []
    return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary


def _is_pure_crew_group(group: Any) -> bool:
    inner = group.get("hooks") if isinstance(group, dict) else None
    return (
        isinstance(inner, list) and bool(inner) and all(isinstance(h, dict) and is_crew_command(h.get("command")) for h in inner)
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Cursor: {"version": 1, "hooks": {"<event>": [{"command": ...}]}}
# ---------------------------------------------------------------------------


def render_cursor_hooks(
    before: str | None, spec: CrewSpec, cmds: CrewCommands, *, remove: bool = False, path_hint: str = "config"
) -> tuple[str, list[str]]:
    data = _load_json_object(before, path_hint)
    new: dict[str, Any] = copy.deepcopy(data)
    hooks = new.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError(f"'hooks' in {path_hint} is not an object")
    wanted: dict[str, list[dict[str, Any]]] = {}
    if not remove:
        for hook in spec.hooks:
            wanted.setdefault(hook.event, []).append({"command": cmds.command(spec.adapter, hook)})
    summary: list[str] = []
    for event in sorted(set(hooks) | set(wanted)):
        current = hooks.get(event)
        entries: list[Any] = list(current) if isinstance(current, list) else []

        def ours(entry: Any) -> bool:
            command = entry.get("command") if isinstance(entry, dict) else None
            return is_crew_command(command) or (not remove and is_relay_command(command))

        kept = [e for e in entries if not ours(e)]
        mine = [e for e in entries if ours(e)]
        target = wanted.get(event, [])
        if mine == target:
            continue
        for entry in mine:
            summary.append(f"{event}: remove `{entry.get('command')}`")
        for entry in target:
            summary.append(f"{event}: add `{entry['command']}`")
        if kept or target:
            hooks[event] = [*kept, *target]
        elif event in hooks:
            del hooks[event]
    if hooks:
        new["hooks"] = hooks
        new.setdefault("version", 1)
    else:
        new.pop("hooks", None)
    if new == data and before is not None:
        return before, []
    return json.dumps(new, indent=2, ensure_ascii=False) + "\n", summary


# ---------------------------------------------------------------------------
# Kimi: [[hooks]] tables in a marked block of config.toml
# ---------------------------------------------------------------------------


def _cut_block(text: str, begin: str, end: str) -> tuple[str, bool]:
    if begin in text and end in text.split(begin, 1)[1]:
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        return head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n"), True
    return text, False


def render_kimi_toml(before: str | None, spec: CrewSpec, cmds: CrewCommands, *, remove: bool = False) -> tuple[str, list[str]]:
    text = before or ""
    summary: list[str] = []
    rest, had_crew = _cut_block(text, KIMI_BEGIN, KIMI_END)
    if not remove:
        rest, had_relay = _cut_block(rest, KIMI_RELAY_BEGIN, KIMI_RELAY_END)
        if had_relay:
            summary.append("remove the remembra-relay [[hooks]] block (crew start/end run the relay brief and close)")
        lines = [KIMI_BEGIN]
        for hook in spec.hooks:
            lines += [
                "[[hooks]]",
                f"event = {json.dumps(hook.event)}",
                f"command = {json.dumps(cmds.command(spec.adapter, hook))}",
                "",
            ]
        block = "\n".join(lines).rstrip() + "\n" + KIMI_END + "\n"
        new = (rest.rstrip() + "\n\n" if rest.strip() else "") + block
    else:
        new = rest
    if new == text:
        return text, []
    if remove:
        summary.append("remove the remembra-crew [[hooks]] block")
    else:
        events = ", ".join(dict.fromkeys(h.event for h in spec.hooks))
        summary.append(f"{'rewrite' if had_crew else 'write'} the remembra-crew [[hooks]] block ({events})")
    return new, summary


def render(
    before: str | None,
    spec: CrewSpec,
    cmds: CrewCommands,
    *,
    remove: bool = False,
    legacy_markers: tuple[str, ...] = (),
    path_hint: str = "config",
) -> tuple[str, list[str]]:
    if spec.style == "json-hooks":
        return render_json_hooks(before, spec, cmds, remove=remove, legacy_markers=legacy_markers, path_hint=path_hint)
    if spec.style == "cursor":
        return render_cursor_hooks(before, spec, cmds, remove=remove, path_hint=path_hint)
    return render_kimi_toml(before, spec, cmds, remove=remove)


def crew_specs() -> dict[str, CrewSpec]:
    """Every adapter's crew spec, Claude Code first (imported lazily: the adapter modules import this one)."""
    from remembra.relay.adapters import claude_code, codex, cursor, gemini, kimi, qwen

    return {m.CREW.adapter: m.CREW for m in (claude_code, codex, cursor, gemini, qwen, kimi)}
