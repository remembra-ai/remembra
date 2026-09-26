"""Which agent is really running a relay hook: routing hooks another agent imported.

Several agents run other agents' hook files, so a relay hook written for one
agent also fires in sessions of another:

- Grok Build loads ``~/.claude/settings.json`` hooks and ``~/.cursor/hooks.json``
  by default (grok-build docs 05-configuration.md "harness compatibility",
  10-hooks.md); its ``/import-claude`` copies the Claude hooks into
  ``~/.grok/config.toml``.
- Cursor, the IDE and the cursor-agent CLI, loads the user's Claude Code
  hooks and runs SessionStart / SessionEnd / PreCompact with Cursor's own
  payload (seen in a run of cursor-agent 2026.09.26's hook runner). The
  payload has no ``cwd`` and user-level Claude hooks run in ``~/.claude``.
- Devin reads ``.claude`` hooks while ``read_config_from.claude`` is on (the
  default), and Continue's ``cn`` reads ``~/.claude/settings.json`` too.
- ``gemini hooks migrate`` and ``kimi migrate`` copy hooks with
  ``--hook claude-code --agent claude-code`` (or the old Kimi hooks) baked in.

Without a check each of those sessions was filed as the hook's own agent
(``claude-code``), read with that agent's payload mapping: a Cursor session
landed under a project named after ``~/.claude``, and a Grok session was
parsed as a Claude Code transcript. :func:`detect_host` names the agent that
runs a hook from markers only that agent sets; ``remembra-relay brief`` /
``close`` then route the hook (``remembra.relay.cli.route_hook``).

Markers, and where each was seen:

========== ============================================ =====================================
host       marker                                       source
========== ============================================ =====================================
grok       env ``GROK_WORKSPACE_ROOT``, payload          xai-grok-hooks runner/command.rs (set
           ``workspaceRoot``                            after the hook's own env, so a hook
                                                        cannot spoof it); event.rs envelope
cursor     payload ``cursor_version``                   recorded (cursor-agent 2026.09.26)
kimi       payload ``client_type == "kimi_code_cli"``   recorded (Kimi Code 2.1.1)
gemini     env ``GEMINI_SESSION_ID``                    recorded (Gemini CLI 0.61.0)
qwen       env ``QWEN_CODE_SESSION_ID``                 recorded (Qwen Code 0.24.6)
devin      env ``DEVIN_PROJECT_DIR``                    Devin CLI hook docs; not seen at runtime
continue   env ``CONTINUE_PROJECT_DIR``                 continuedev/continue hookRunner.ts
(any)      the payload's transcript is in the directory recorded Codex payloads: a rollout
           of an agent the relay has an adapter for         under ``~/.codex/sessions``
========== ============================================ =====================================

The last row catches agents whose payload is otherwise Claude-shaped (Codex
sends exactly Claude's fields; its binary carries an importer for Claude Code
settings). A hook whose transcript lies in its own agent's directory (a
Claude Code transcript under ``~/.claude/projects``) is never foreign,
whatever the environment holds: Qwen Code also sets ``QWEN_CODE_SESSION_ID``
for its shell tool, so a Claude Code session started from Qwen's shell keeps
its own name. VS Code Copilot with ``chat.useClaudeHooks`` has no known marker
yet.

:func:`copied_relay_hooks` finds relay hooks that an import copied into a
config file of an agent that is not the one they were written for, so
``connect`` can say they are there.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from remembra.relay.adapters import REGISTRY
from remembra.relay.adapters.base import Adapter, is_relay_command
from remembra.relay.config_view import loads_jsonc


def _env_set(environ: Mapping[str, str], name: str) -> bool:
    return bool((environ.get(name) or "").strip())


# (host, test(payload, environ)); the first match names the host. Grok comes first: it also runs
# Cursor's and Claude's hook files, and those markers are the ones a Grok payload would not carry.
MARKERS: tuple[tuple[str, Callable[[Mapping[str, Any], Mapping[str, str]], bool]], ...] = (
    ("grok", lambda p, e: _env_set(e, "GROK_WORKSPACE_ROOT") or "workspaceRoot" in p),
    ("cursor", lambda p, e: "cursor_version" in p),
    ("kimi", lambda p, e: p.get("client_type") == "kimi_code_cli"),
    ("gemini", lambda p, e: _env_set(e, "GEMINI_SESSION_ID")),
    ("qwen", lambda p, e: _env_set(e, "QWEN_CODE_SESSION_ID")),
    ("devin", lambda p, e: _env_set(e, "DEVIN_PROJECT_DIR")),
    ("continue", lambda p, e: _env_set(e, "CONTINUE_PROJECT_DIR")),
)

# Transcript keys in hook payloads (Grok sends both spellings).
_TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")


def _own_dirs(adapter: Adapter, environ: Mapping[str, str], home: Path) -> list[Path]:
    """Where the hook's own agent keeps its files: ``~/.claude``, and ``$CLAUDE_CONFIG_DIR`` when set."""
    dirs = [adapter.spec.config_home(home)]
    moved = (environ.get(adapter.spec.home_env) or "").strip() if adapter.spec.home_env else ""
    if moved:
        dirs.append(adapter.spec.dir_from_env(moved))
    return dirs


def _inside(path: str, root: Path) -> bool:
    """True when ``path`` is under ``root``, as written or with symlinks resolved; never raises."""
    try:
        candidate = Path(path).expanduser()
        if candidate.is_relative_to(root):
            return True
        return Path(os.path.realpath(candidate)).is_relative_to(os.path.realpath(root))
    except (OSError, ValueError, RuntimeError):  # RuntimeError: "~user" of an unknown user
        return False


def _transcripts(adapter: Adapter, payload: Mapping[str, Any]) -> list[str]:
    keys = dict.fromkeys((*_TRANSCRIPT_KEYS, *adapter.spec.payload.transcript))
    return [value.strip() for key in keys if isinstance(value := payload.get(key), str) and value.strip()]


def own_transcript(adapter: Adapter, payload: Mapping[str, Any], environ: Mapping[str, str], home: Path) -> bool:
    """True when the payload's transcript is in ``adapter``'s agent's own directory: that agent's session."""
    roots = _own_dirs(adapter, environ, home)
    return any(_inside(path, root) for path in _transcripts(adapter, payload) for root in roots)


def detect_host(
    adapter: Adapter,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
    home: Path,
    adapters: Mapping[str, Adapter] | None = None,
) -> str | None:
    """The agent running this ``--hook <adapter>`` hook when that is another agent, else None.

    ``adapters`` (default: the registry) are the agents a transcript location can name.
    """
    if own_transcript(adapter, payload, environ, home):
        return None  # e.g. a real Claude Code session: never foreign
    host = next((name for name, test in MARKERS if test(payload, environ)), None)
    if host is None:
        others = (a for name, a in (adapters if adapters is not None else REGISTRY).items() if name != adapter.spec.name)
        host = next((a.spec.name for a in others if own_transcript(a, payload, environ, home)), None)
    return host if host != adapter.spec.name else None


# ---------------------------------------------------------------------------
# Relay hooks copied into another agent's config
# ---------------------------------------------------------------------------

# How copies reach each host's file.
COPIED_BY = {
    "grok": "Grok Build's /import-claude",
    "gemini": "`gemini hooks migrate`",
    "kimi": "`kimi migrate`",
}

_HOOK_ARG_RE = re.compile(r"\s(?:brief|close) --hook ([\w.-]+)")


@dataclass(frozen=True)
class CopiedHooks:
    """Relay hooks in ``path`` (a config file ``host`` runs) that were written for other agents."""

    path: Path
    host: str
    hooks: dict[str, int]  # --hook name -> how many

    @property
    def total(self) -> int:
        return sum(self.hooks.values())


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _parse(path: Path) -> Any:
    """The file's content (JSON with comments, or TOML), or None when it is missing or unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        if path.suffix.lower() == ".toml":
            return tomllib.loads(text)
        return loads_jsonc(text)[0] if text.strip() else None
    except ValueError:  # tomllib.TOMLDecodeError and json.JSONDecodeError are ValueErrors
        return None


def host_files(home: Path, adapters: Mapping[str, Adapter]) -> list[tuple[str, Path]]:
    """(host, file) pairs whose relay hooks :func:`copied_relay_hooks` checks.

    Every adapter's own file (run by that agent), plus the files imports write
    for agents the relay has no adapter file for: Grok Build's
    ``config.toml`` (``$GROK_HOME``) and Kimi Code's (``$KIMI_CODE_HOME``). The
    variables are read only for the real home, as in :meth:`AdapterSpec.moved_home`.
    """
    real_home = Path(home) == Path.home()

    def moved(env: str, default: str) -> Path:
        value = os.environ.get(env, "").strip() if real_home else ""
        return Path(value).expanduser() if value else Path(home) / default

    pairs = [(name, adapter.spec.config_file(home)) for name, adapter in adapters.items()]
    for host, path in (
        ("grok", moved("GROK_HOME", ".grok") / "config.toml"),
        ("kimi", moved("KIMI_CODE_HOME", ".kimi-code") / "config.toml"),
    ):
        if all(path != known for _, known in pairs):
            pairs.append((host, path))
    return pairs


def copied_relay_hooks(home: Path, adapters: Mapping[str, Adapter]) -> list[CopiedHooks]:
    """Relay hooks written for one agent that sit in a config file another agent runs.

    ``connect`` never writes these: an import copied them (:data:`COPIED_BY`).
    :func:`detect_host` already keeps them from filing sessions under the wrong
    agent; they are reported so the user can delete them.
    """
    found: list[CopiedHooks] = []
    for host, path in host_files(home, adapters):
        data = _parse(path)
        if data is None:
            continue
        counts: dict[str, int] = {}
        for command in _strings(data):
            if not is_relay_command(command):
                continue
            match = _HOOK_ARG_RE.search(command)
            if match and match.group(1) != host:
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        if counts:
            found.append(CopiedHooks(path=path, host=host, hooks=counts))
    return found
