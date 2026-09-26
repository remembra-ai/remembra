"""Codex hook trust, read from files only: does Codex run the remembra-relay hooks?

Codex runs a user hook only after the user trusts it (``/hooks`` in the CLI,
Settings > Hooks in the app). Trust is stored in ``~/.codex/config.toml``::

    [hooks.state."/Users/me/.codex/hooks.json:session_start:0:0"]
    trusted_hash = "sha256:5fd9…"

The key is ``<hooks.json path>:<event>:<group index>:<handler index>``; the
hash identifies the hook's normalized config, so any change to the command,
timeout or matcher makes the old record stale (Codex reports it "modified"
and skips the hook until it is trusted again). A record with
``enabled = false`` turns the hook off.

:func:`hook_hash` recomputes that hash the way Codex does (``hook_hash`` in
``codex-rs/hooks/src/engine/discovery.rs`` and ``version_for_toml`` in
``codex-rs/config/src/fingerprint.rs``): the identity
``{"event_name": <event key>, "matcher"?: …, "hooks": [<normalized handler>]}``
serialized as JSON with sorted keys and no spaces, then SHA-256. It was
checked against ``codex app-server`` ``hooks/list`` on codex-cli
0.155.0-alpha.16.4 and 0.157.1 (see ``tests/fixtures/marshal/codex_trust/``).
Because the layout is Codex-internal, only a matching hash counts as trusted;
a hash that differs is reported as inferred, and a ``config.toml`` that cannot
be read is "unchecked", never trusted.

Nothing here writes, and nothing but ``hooks.state`` is taken from
``config.toml`` (which also holds the Remembra key in the MCP server's env).
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import is_relay_command

HASH_VERIFIED_WITH = ("0.155.0-alpha.16.4", "0.157.1")

# HookEventName -> the label Codex uses in keys and hashes (hook_event_key_label).
EVENT_KEYS: dict[str, str] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
# Events whose matcher is dropped before hashing (matcher_pattern_for_event).
_NO_MATCHER = frozenset({"UserPromptSubmit", "Stop", "Interrupt"})
# SessionEnd / Interrupt default to 1 s and are capped at 3 s; others default to 600 s.
_SHORT_TIMEOUT = frozenset({"SessionEnd", "Interrupt"})
_CONTEXT_LIMIT_EVENTS = frozenset({"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"})
_DEFAULT_CONTEXT_LIMIT = 2500
_KNOWN_FIELDS = frozenset(
    {"type", "command", "timeout", "async", "statusMessage", "commandWindows", "command_windows", "additionalContextLimit"}
)

TRUSTED = "trusted"
MODIFIED = "modified"
UNTRUSTED = "untrusted"
DISABLED = "disabled"
UNCHECKED = "unchecked"


def _timeout(event: str, value: Any) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        return None
    if event in _SHORT_TIMEOUT:
        return min(max(value if value is not None else 1, 1), 3)
    return max(value if value is not None else 600, 1)


def hook_hash(event: str, matcher: Any, hook: Any) -> str | None:
    """``sha256:<hex>`` as Codex computes it for one command hook; None when it cannot be computed here."""
    if event not in EVENT_KEYS or not isinstance(hook, dict) or hook.get("type") != "command":
        return None
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    timeout = _timeout(event, hook.get("timeout"))
    if timeout is None:
        return None
    handler: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout, "async": bool(hook.get("async", False))}
    status = hook.get("statusMessage")
    if status is not None:
        if not isinstance(status, str):
            return None
        handler["statusMessage"] = status
    limit = hook.get("additionalContextLimit")
    if limit is not None and event in _CONTEXT_LIMIT_EVENTS and limit != _DEFAULT_CONTEXT_LIMIT:
        if isinstance(limit, bool) or not isinstance(limit, int):
            return None
        handler["additionalContextLimit"] = limit
    identity: dict[str, Any] = {"event_name": EVENT_KEYS[event], "hooks": [handler]}
    if isinstance(matcher, str) and event not in _NO_MATCHER:
        identity["matcher"] = matcher
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RelayHook:
    event: str
    group: int
    index: int
    command: str
    hash: str | None  # None: a field this module does not model (never reported as a mismatch)
    certain: bool  # every field of the hook is one the hash above covers

    @property
    def key_suffix(self) -> str:
        return f"{EVENT_KEYS[self.event]}:{self.group}:{self.index}"


@dataclass(frozen=True)
class HookTrust:
    hook: RelayHook
    status: str  # trusted | modified | untrusted | disabled | unchecked
    proven: bool


@dataclass(frozen=True)
class CodexTrust:
    hooks_path: Path
    config_path: Path
    hooks_readable: bool
    relay_hooks: tuple[RelayHook, ...]
    config_state: str  # "missing" | "unreadable" | "read"
    config_error: str | None
    state_entries: int  # all [hooks.state] records, any file
    per_hook: tuple[HookTrust, ...] = field(default_factory=tuple)

    def statuses(self) -> set[str]:
        return {t.status for t in self.per_hook}

    @property
    def all_trusted(self) -> bool:
        return bool(self.per_hook) and all(t.status == TRUSTED for t in self.per_hook)


def relay_hooks_in(text: str | None) -> tuple[bool, tuple[RelayHook, ...]]:
    """``(readable, remembra-relay hooks)`` in a Codex hooks.json text (Claude-style nesting)."""
    if text is None:
        return True, ()
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return False, ()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return isinstance(data, dict), ()
    found: list[RelayHook] = []
    for event, groups in hooks.items():
        if event not in EVENT_KEYS or not isinstance(groups, list):
            continue
        for g, group in enumerate(groups):
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                continue
            for i, hook in enumerate(inner):
                command = hook.get("command") if isinstance(hook, dict) else None
                if not is_relay_command(command):
                    continue
                certain = isinstance(hook, dict) and set(hook) <= _KNOWN_FIELDS and not hook.get("commandWindows")
                found.append(
                    RelayHook(
                        event=event,
                        group=g,
                        index=i,
                        command=str(command),
                        hash=hook_hash(event, group.get("matcher"), hook),
                        certain=certain,
                    )
                )
    return True, tuple(found)


def _same_file(a: str, b: Path) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def trust_for(
    hooks_path: Path,
    hooks_text: str | None,
    config_path: Path,
    config_text: str | None,
    config_error: str | None = None,
) -> CodexTrust:
    """Trust of the relay hooks in ``hooks_text`` (as written, or as ``connect`` would write it)."""
    readable, relay = relay_hooks_in(hooks_text)
    if config_error is not None:
        state = "unreadable"
        records: dict[str, Any] = {}
    elif config_text is None:
        state = "missing"
        records = {}
    else:
        try:
            data = tomllib.loads(config_text)
            hooks_table = data.get("hooks")
            raw = hooks_table.get("state") if isinstance(hooks_table, dict) else None
            records = raw if isinstance(raw, dict) else {}
            state = "read"
        except ValueError as e:  # tomllib.TOMLDecodeError
            state, records, config_error = "unreadable", {}, f"not valid TOML ({e.__class__.__name__})"
    ours: dict[str, dict[str, Any]] = {}
    for key, value in records.items():
        # key = <path>:<event>:<group>:<index>; split the last three fields off the path
        parts = str(key).rsplit(":", 3)
        if len(parts) != 4 or not isinstance(value, dict):
            continue
        if _same_file(parts[0], hooks_path):
            ours[":".join(parts[1:])] = value
    per_hook: list[HookTrust] = []
    for hook in relay:
        if state == "unreadable":
            per_hook.append(HookTrust(hook, UNCHECKED, proven=False))
            continue
        record = ours.get(hook.key_suffix)
        if record is None:
            per_hook.append(HookTrust(hook, UNTRUSTED, proven=True))
        elif record.get("enabled") is False:
            per_hook.append(HookTrust(hook, DISABLED, proven=True))
        elif record.get("trusted_hash") is None:
            per_hook.append(HookTrust(hook, UNTRUSTED, proven=True))
        elif hook.hash is not None and record.get("trusted_hash") == hook.hash:
            per_hook.append(HookTrust(hook, TRUSTED, proven=True))
        elif hook.hash is None or not hook.certain:
            per_hook.append(HookTrust(hook, UNCHECKED, proven=False))
        else:
            per_hook.append(HookTrust(hook, MODIFIED, proven=False))
    return CodexTrust(
        hooks_path=hooks_path,
        config_path=config_path,
        hooks_readable=readable,
        relay_hooks=relay,
        config_state=state,
        config_error=config_error,
        state_entries=len(records),
        per_hook=tuple(per_hook),
    )


def _read(path: Path) -> tuple[str | None, str | None]:
    """``(text, error)``: text None + error None when the file does not exist."""
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as e:
        return None, e.__class__.__name__


def read_trust(home: Path, hooks_text: str | None = None, *, planned: bool = False, hooks_path: Path | None = None) -> CodexTrust:
    """Trust of the relay hooks in ``hooks.json`` (or in ``hooks_text`` when ``planned``).

    ``hooks_path`` defaults to ``~/.codex/hooks.json``; Codex keeps ``config.toml`` next to it.
    """
    hooks_path = hooks_path or Path(home) / ".codex" / "hooks.json"
    config_path = hooks_path.with_name("config.toml")
    if not planned:
        hooks_text, _ = _read(hooks_path)
    config_text, error = _read(config_path)
    return trust_for(hooks_path, hooks_text, config_path, config_text, error)
