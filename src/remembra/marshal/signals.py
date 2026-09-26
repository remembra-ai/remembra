"""What Marshal reads before it says anything: this machine's relay setup and, optionally, your trail.

:func:`collect` never writes. It reads:

- the relay config (:func:`remembra.relay.config.load_config`, shown only
  through ``RelayConfig.redacted()``), once as the hooks of each agent see it;
- the outbox as files (``outbox.pending`` is NOT used: it renames stale claims
  and unreadable entries), ``status.json`` and the detached-close log tail;
- each adapter's config file (which relay hooks are there, whether the binary
  they call still exists, a ``*.bak-relay-*`` backup ``connect --apply`` left);
- the ``remembra`` MCP server entry per agent (``tools/doctor.py`` parsers,
  project only; never ``run_remote_checks``, which spends a recall);
- Codex hook trust (:mod:`remembra.marshal.codex_hooks`) and the first line of
  recent Codex rollouts (automation runs);
- with ``check_server``: at most :data:`MAX_GETS` GET requests to
  ``/api/v1/trail/summary`` and ``/api/v1/trail`` with the user's own key.
  Nothing else is ever requested (no brief: it records a pickup; no recall,
  no resolve, no write).

Values that come from the server are reduced to agent labels, counts, entry
types and times; handoff text never enters a signal.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from remembra.client.project import normalize_project_id, parse_project_aliases
from remembra.marshal import codex_hooks, words
from remembra.relay import outbox
from remembra.relay.adapters import REGISTRY, Adapter
from remembra.relay.adapters.base import is_relay_command
from remembra.relay.config import RelayConfig, load_config, load_config_from_source
from remembra.security.secrets import redact_secrets
from remembra.security.untrusted import strip_hidden

MAX_GETS = 4
HTTP_TIMEOUT_SECONDS = 8.0
TOTAL_HTTP_SECONDS = 20.0
TRAIL_WINDOW = 100
ALLOWED_GETS = frozenset({"/api/v1/trail/summary", "/api/v1/trail"})
AUTOMATION_SCAN_DAYS = 7
AUTOMATION_SCAN_MAX_FILES = 500
FIRST_LINE_MAX_BYTES = 1024 * 1024
CLOSE_LOG_TAIL_LINES = 20
STALE_CHECKPOINT_MINUTES = 60

_AGENT_LABEL_RE = re.compile(r"[^a-z0-9._-]+")
_RELAY_CMD_RE = re.compile(r"^(?P<prefix>.+?) (?:brief|close) --hook (?P<hook>[\w.-]+) --agent \S+(?: --once)?$")
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_RAY_RE = re.compile(r"Ray ID:?\s*(?:<[^>]*>\s*)*([0-9a-f]{16})", re.I)
_TRUE = frozenset({"1", "true", "yes", "on"})
_MCP_ENV_KEYS = ("REMEMBRA_API_KEY", "REMEMBRA_URL", "REMEMBRA_PROJECT", "REMEMBRA_AGENT_ID", "REMEMBRA_USER_ID")


def _version() -> str:
    try:
        from remembra import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - the package always has one
        return "unknown"


def config_file(adapter: Adapter, home: Path) -> Path:
    """The hook file an adapter writes: ``spec.config_file(home)`` where the relay has it (it follows
    ``CODEX_HOME`` and similar), else ``spec.config_path(home)``."""
    spec: Any = adapter.spec
    resolver = getattr(spec, "config_file", None)
    return Path(resolver(home) if callable(resolver) else spec.config_path(home))


def agent_label(value: Any) -> str | None:
    """An agent id reduced to ``[a-z0-9._-]`` (server-sent labels never carry other characters into a slip).

    Other spellings of a known agent ("claude", "codex-cli") become its id first, as the dashboard counts them.
    """
    if not isinstance(value, str):
        return None
    label = _AGENT_LABEL_RE.sub("", words.canonical_agent(value))[:64]
    return label or None


def tilde(path: Path | str, home: Path) -> str:
    """``path`` with the home directory shown as ``~`` (no user name in a slip)."""
    text = str(path)
    root = str(home).rstrip("/")
    if root and (text == root or text.startswith(root + "/")):
        return "~" + text[len(root) :]
    return text


def clean_text(text: str, limit: int = 160) -> str:
    """One line of local text made safe to show: hidden characters stripped, secrets redacted, cut."""
    visible, _, _ = strip_hidden(str(text))
    redacted = redact_secrets(visible).text
    one = " ".join(redacted.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


# Shown instead of a configured server URL that is not an http(s) URL with a host: whatever is there (a key
# pasted into the URL field, for one) is never printed.
NOT_A_URL = "(the server URL is not a URL)"


def display_url(url: str | None) -> str:
    """A server URL as a slip may show it: an http(s) URL with a host, without credentials, query or fragment,
    and with anything key-shaped redacted; :data:`NOT_A_URL` for anything else."""
    cleaned = outbox.clean_url(url) if url else None
    if cleaned:
        try:
            parts = urlsplit(cleaned)
        except ValueError:
            return NOT_A_URL
        if parts.scheme in ("http", "https") and parts.hostname:
            return clean_text(cleaned, 200)
    return NOT_A_URL


def parse_time(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def slot_ts(slot: Any) -> float | None:
    """The ``ts`` of a status.json slot (``last_success`` / ``last_failure``), or None."""
    value = slot.get("ts") if isinstance(slot, Mapping) else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def is_firewall_block(status: int | None, text: str | None, headers: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """``(blocked, ray id)``: a 403 that came from a firewall in front of the API (an HTML page), not from Remembra.

    Remembra answers errors in JSON. A 403 whose body is HTML, or that carries
    Cloudflare's ``cf-ray`` / ``server: cloudflare`` headers, was written by
    the proxy: the key was never checked.
    """
    if status != 403:
        return False, None
    head = (text or "").lstrip()[:4000]
    lower = head.lower()
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    ray = hdrs.get("cf-ray")
    match = _RAY_RE.search(head)
    if not ray and match:
        ray = match.group(1)
    cloudflare = "cloudflare" in (hdrs.get("server") or "").lower() or bool(hdrs.get("cf-ray")) or "cloudflare" in lower
    html = lower.startswith("<!doctype html") or lower.startswith("<html") or "<head" in lower[:600]
    if cloudflare or html:
        return True, (ray.split("-")[0] if ray else None)
    return False, None


# ---------------------------------------------------------------------------
# Signal records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Read:
    what: str
    result: str
    ms: int | None = None
    ok: bool = True


@dataclass(frozen=True)
class KeyCheck:
    source: str  # shown with ~ for the home directory
    url: str
    state: str  # missing | accepted | rejected | refused | firewall | unchecked | skipped
    agents: tuple[str, ...]
    http_status: int | None = None
    error: str | None = None
    ray_id: str | None = None
    recorded: Mapping[str, Any] | None = None  # status.json keys[source]: what the hooks last got
    primary: bool = False


@dataclass(frozen=True)
class OutboxEntry:
    file: str
    agent: str
    session: str
    attempts: int
    queued_ts: float
    url: str | None
    config_source: str | None
    last_error: str
    http_status: int | None
    error_class: str  # rejected | refused | firewall | rate_limited | network | no_key | server | other
    held: str | None
    ray_id: str | None = None


@dataclass(frozen=True)
class AgentSignals:
    name: str
    display: str
    verified: bool
    detected: bool
    config_path: str
    config_readable: bool
    expected_events: tuple[str, ...]
    present_events: tuple[str, ...]
    core_events: tuple[str, ...]
    outdated: bool  # every hook is there, but connect would still rewrite them (older connect)
    relay_prefix: str | None  # what the hooks run before `brief`/`close`: for planning only, never shown (it may hold a key)
    missing_binary: str | None  # the hooks call this, and it no longer exists (cleaned for display)
    backup: str | None  # a *.bak-relay-* next to the config: connect --apply wrote here once
    mcp: str  # configured | missing | unknown
    mcp_path: str | None
    mcp_project: str | None
    key_source: str
    last_success: Mapping[str, Any] | None
    last_failure: Mapping[str, Any] | None
    detach_close: bool
    notes: str
    setup_note: str

    @property
    def hooks_written(self) -> bool:
        return bool(self.expected_events) and set(self.expected_events) <= set(self.present_events)

    @property
    def core_written(self) -> bool:
        return set(self.core_events) <= set(self.present_events)

    @property
    def any_hooks(self) -> bool:
        return bool(self.present_events)

    @property
    def missing_events(self) -> tuple[str, ...]:
        return tuple(e for e in self.expected_events if e not in self.present_events)


@dataclass(frozen=True)
class CodexSignals:
    trust: codex_hooks.CodexTrust | None
    automations_7d: int
    subagents_7d: int
    rollouts_scanned: int
    skip_supported: bool
    include_automations: bool
    skipped_logged: int


@dataclass(frozen=True)
class CloseLog:
    path: str
    mtime: float | None
    tail: tuple[str, ...]
    has_error: bool


@dataclass(frozen=True)
class ServerSignals:
    url: str
    entries_7d: Mapping[str, int]
    handoffs_7d: Mapping[str, int]
    last_active: Mapping[str, float]
    trail_read: bool
    last_entry: Mapping[str, tuple[str, float]]  # agent -> (memory_type, created_at) of its newest entry
    pickups_by: Mapping[str, int]  # entries in the window this agent picked up (one per entry, as the slip counts)
    last_pickup: Mapping[str, float]
    handoffs: tuple[tuple[str, float, tuple[tuple[str, float], ...]], ...]  # (author, at, pickups), newest first
    handoffs_all: Mapping[str, int] = field(default_factory=dict)  # the summary's all-time handoff count per agent

    @property
    def baton(self) -> tuple[str, float, tuple[tuple[str, float], ...]] | None:
        """The newest handoff in the window: who left it, when, and who picked it up."""
        return self.handoffs[0] if self.handoffs else None

    def waiting_for(self, agent: str, since: float | None = None) -> int:
        """Handoffs from other agents in the window (left after ``since``, when given) that ``agent`` has not picked up."""
        return sum(
            1
            for author, at, picks in self.handoffs
            if author != agent and (since is None or at > since) and agent not in {r for r, _ in picks}
        )

    def handed_off(self, agent: str) -> bool:
        """Any handoff from ``agent`` is known: in the summary's all-time count, this week's, or the trail read."""
        last = self.last_entry.get(agent)
        return bool(
            self.handoffs_all.get(agent, 0)
            or self.handoffs_7d.get(agent, 0)
            or any(author == agent for author, _, _ in self.handoffs)
            or (last is not None and last[0] == "handoff")
        )

    def handoff_since(self, agent: str, since: float) -> float | None:
        """The newest handoff from ``agent`` the trail shows after ``since`` (a close that worked), or None."""
        times = [at for author, at, _ in self.handoffs if author == agent and at > since]
        last = self.last_entry.get(agent)
        if last is not None and last[0] == "handoff" and last[1] > since:
            times.append(last[1])
        return max(times, default=None)


@dataclass(frozen=True)
class Signals:
    version: str
    generated_at: str
    now: float
    home: Path
    repo: tuple[str, str | None] | None
    config: Mapping[str, Any]
    wanted: tuple[str, ...]
    keys: tuple[KeyCheck, ...]
    outbox: tuple[OutboxEntry, ...]
    outbox_unreadable: tuple[str, ...]
    status_agents: Mapping[str, Any]
    agents: Mapping[str, AgentSignals]
    codex: CodexSignals
    close_log: CloseLog | None
    namespace: tuple[str, str] | None  # (project, where it is configured)
    mcp_projects: Mapping[str, str]
    server: ServerSignals | None
    check_server: bool
    reads: tuple[Read, ...]
    unchecked: tuple[str, ...]
    server_url: str
    missing_key_source: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def key_for(self, agent: str) -> KeyCheck | None:
        for key in self.keys:
            if agent in key.agents:
                return key
        return next((k for k in self.keys if k.primary), None)


# ---------------------------------------------------------------------------
# Local reads
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> tuple[str | None, bool]:
    """``(text, readable)``; a missing file is ``(None, True)``."""
    try:
        return path.read_text(encoding="utf-8"), True
    except FileNotFoundError:
        return None, True
    except (OSError, UnicodeDecodeError):
        return None, False


def _relay_events(data: Any, toml_hooks: bool) -> dict[str, list[str]]:
    """Relay commands per hook event, for the three config shapes the adapters write."""
    events: dict[str, list[str]] = {}
    if toml_hooks:  # Kimi: [[hooks]] tables with event / command
        tables = data.get("hooks") if isinstance(data, dict) else None
        for table in tables if isinstance(tables, list) else []:
            if isinstance(table, dict) and is_relay_command(table.get("command")):
                events.setdefault(str(table.get("event")), []).append(str(table["command"]))
        return events
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return events
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if isinstance(inner, list):
                commands = [h.get("command") for h in inner if isinstance(h, dict)]
            else:
                commands = [entry.get("command")]
            for command in commands:
                if is_relay_command(command):
                    events.setdefault(str(event), []).append(str(command))
    return events


def _prefix(commands: list[str], name: str) -> str | None:
    counts: dict[str, int] = {}
    for command in commands:
        match = _RELAY_CMD_RE.match(command.strip())
        if match and match.group("hook") == name:
            counts[match.group("prefix")] = counts.get(match.group("prefix"), 0) + 1
    return max(counts, key=lambda p: counts[p]) if counts else None


def _missing_binary(prefix: str | None) -> str | None:
    if not prefix:
        return None
    try:
        first = shlex.split(prefix)[0]
    except (ValueError, IndexError):
        return None
    if os.path.isabs(first) and not os.path.exists(first):
        return first
    return None


def _backup(path: Path) -> str | None:
    try:
        found = sorted(path.parent.glob(f"{path.name}.bak-relay-*"))
    except OSError:
        return None
    return found[-1].name if found else None


_MCP_FILES: dict[str, tuple[str, str]] = {
    "claude-code": ("claude_code", ".claude.json"),
    "codex": ("codex", ".codex/config.toml"),
    "cursor": ("cursor", ".cursor/mcp.json"),
    "gemini": ("gemini", ".gemini/settings.json"),
    "qwen": ("qwen", ".qwen/settings.json"),
}


def _mcp_entry(name: str, home: Path, environ: Mapping[str, str]) -> tuple[str, str | None, str | None]:
    """``(configured | missing | unknown, config path, REMEMBRA_PROJECT)`` of the agent's remembra MCP server."""
    from remembra.tools import doctor as tools_doctor

    spec = _MCP_FILES.get(name)
    if spec is None:
        return "unknown", None, None
    kind, rel = spec
    if name == "claude-code":
        path = Path(environ.get("REMEMBRA_HOOK_CLAUDE_CONFIG") or home / rel)
    elif name == "codex":  # next to hooks.json: Codex keeps both in CODEX_HOME
        path = config_file(REGISTRY["codex"], home).with_name("config.toml")
    else:
        path = home / rel
    try:
        if kind == "codex":
            target = tools_doctor.load_codex_target(path)
        elif kind == "claude_code":
            target = tools_doctor.load_claude_code_target(path)
        elif kind == "qwen":
            target = tools_doctor.load_json_agent_target("qwen", path)
        else:
            target = getattr(tools_doctor, f"load_{kind}_target")(path)
    except tools_doctor.DoctorError as e:
        reason = str(e)
        if reason.startswith(("config_missing", "missing_server")):
            return "missing", str(path), None
        return "unknown", str(path), None
    except Exception:
        return "unknown", str(path), None
    return "configured", str(path), target.project


def _agent_signals(
    name: str,
    adapter: Adapter,
    home: Path,
    which: Callable[[str], str | None],
    environ: Mapping[str, str],
    status_agents: Mapping[str, Any],
    key_source: str,
) -> AgentSignals:
    spec = adapter.spec
    path = config_file(adapter, home)
    text, readable = _read_text(path)
    events: dict[str, list[str]] = {}
    if text is not None:
        try:
            data = tomllib.loads(text) if path.suffix == ".toml" else (json.loads(text) if text.strip() else {})
            events = _relay_events(data, toml_hooks=path.suffix == ".toml")
        except ValueError:
            readable = False
    expected = tuple(event for _, event in adapter.events())
    core = tuple(e for e in (spec.start_event, spec.end_event) if e)
    commands = [c for cs in events.values() for c in cs]
    prefix = _prefix(commands, name)
    outdated = False
    if prefix and set(expected) <= set(events):
        try:
            outdated = adapter.plan(home, prefix).changed
        except Exception:
            outdated = False
    try:
        detected = adapter.detect(home, which)
    except Exception:
        detected = False
    mcp, mcp_path, mcp_project = _mcp_entry(name, home, environ)
    slot = status_agents.get(name) if isinstance(status_agents.get(name), dict) else {}
    return AgentSignals(
        name=name,
        display=words.agent_name(name),
        verified=spec.verified,
        detected=detected,
        config_path=tilde(path, home),
        config_readable=readable,
        expected_events=expected,
        present_events=tuple(e for e in expected if e in events),
        core_events=core,
        outdated=outdated,
        relay_prefix=prefix,
        missing_binary=clean_text(missing, 120) if (missing := _missing_binary(prefix)) else None,
        backup=_backup(path),
        mcp=mcp,
        mcp_path=tilde(mcp_path, home) if mcp_path else None,
        mcp_project=mcp_project,
        key_source=key_source,
        last_success=slot.get("last_success") if isinstance(slot, dict) else None,
        last_failure=slot.get("last_failure") if isinstance(slot, dict) else None,
        detach_close=spec.detach_close,
        notes=spec.notes,
        setup_note=spec.setup_note,
    )


def error_class(error: str, http_status: int | None) -> tuple[str, str | None]:
    """Class of a recorded failure: rejected / refused / firewall / rate_limited / network / no_key / server / other."""
    status = http_status
    if status is None:
        match = _HTTP_STATUS_RE.search(error or "")
        status = int(match.group(1)) if match else None
    if status == 403:
        text = (error or "").split(":", 1)[1] if ":" in (error or "") else error
        blocked, ray = is_firewall_block(403, text)
        return ("firewall", ray) if blocked else ("refused", None)
    if status == 401:
        return "rejected", None
    if status == 429:
        return "rate_limited", None
    if status is not None and status >= 500:
        return "server", None
    if status is not None:
        return "other", None
    lower = (error or "").lower()
    if "no api key" in lower:
        return "no_key", None
    if any(word in lower for word in ("connect", "timeout", "timed out", "unreachable", "network", "name or service", "resolve")):
        return "network", None
    return "other", None


def held_reason(entry: outbox.Entry, environ: Mapping[str, str], home: Path) -> str | None:
    """Why a queued close is not sent (``remembra.relay.cli.replay_config``, with this environment)."""
    recorded = entry.data.get("config_source")
    agent = entry.agent_id or None
    config: RelayConfig | None
    if recorded and recorded != "none":
        config = load_config_from_source(str(recorded), agent=agent, environ=environ)
        if config is None or not config.api_key:
            return f"its key source ({tilde(str(recorded).partition(':')[2] or str(recorded), home)}) has no key now"
    else:
        config = load_config(agent=agent, environ=environ, home=home)
        if not config.api_key:
            return "no API key is configured yet"
    if not entry.url:
        return "it was queued without a server; it is only kept, never sent"
    if outbox.clean_url(config.url) != entry.url:
        return f"it is for {display_url(entry.url)}, and the key now points at {display_url(config.url)}"
    return None


def _outbox(home: Path, environ: Mapping[str, str]) -> tuple[list[OutboxEntry], list[str]]:
    directory = outbox.outbox_dir(home)
    entries: list[OutboxEntry] = []
    unreadable: list[str] = []
    if not directory.is_dir():
        return entries, unreadable
    for path in sorted(directory.glob("*.json")):
        entry = outbox.read_entry(path)
        if entry is None:
            unreadable.append(path.name)
            continue
        error = str(entry.data.get("last_error") or "")
        status = entry.data.get("last_status")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        klass, ray = error_class(error, status)
        entries.append(
            OutboxEntry(
                file=path.name,
                agent=agent_label(entry.agent_id) or "unknown-agent",
                session=clean_text(entry.session_id, 24),
                attempts=entry.attempts,
                queued_ts=entry.queued_ts,
                url=display_url(entry.url) if entry.url else None,
                config_source=source_label(str(entry.data.get("config_source") or "none"), home),
                last_error=clean_text(error),
                http_status=status,
                error_class=klass,
                held=held_reason(entry, environ, home),
                ray_id=ray,
            )
        )
    entries.sort(key=lambda e: (e.queued_ts, e.file))
    return entries, unreadable


def _close_log(home: Path) -> CloseLog | None:
    path = home / ".remembra" / "relay" / "last-detached-close.log"
    try:
        mtime = path.stat().st_mtime
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [clean_text(line, 200) for line in raw.splitlines() if line.strip()][-CLOSE_LOG_TAIL_LINES:]
    has_error = any(("failed" in line.lower() or "traceback" in line.lower() or "error" in line.lower()) for line in lines)
    return CloseLog(path=tilde(path, home), mtime=mtime, tail=tuple(lines), has_error=has_error)


def read_first_line(path: Path, max_bytes: int = FIRST_LINE_MAX_BYTES) -> bytes | None:
    """First line of a regular file (opened without blocking); None for anything else. Never raises."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        line = bytearray()
        while len(line) <= max_bytes:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            cut = chunk.find(b"\n")
            if cut >= 0:
                line += chunk[:cut]
                break
            line += chunk
        return bytes(line) if line.strip() and len(line) <= max_bytes else None
    except OSError:
        return None
    finally:
        os.close(fd)


def thread_kind(first_line: bytes | None) -> str | None:
    """``automation`` / ``subagent`` from a Codex rollout's ``session_meta`` line; None for anything else."""
    if not first_line:
        return None
    try:
        record = json.loads(first_line)
    except (ValueError, RecursionError):
        return None
    payload = record.get("payload") if isinstance(record, dict) and record.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    source = payload.get("thread_source")
    if isinstance(source, str) and source.strip().lower() in ("automation", "subagent"):
        return source.strip().lower()
    if isinstance(payload.get("source"), dict) and "subagent" in payload["source"]:
        return "subagent"
    parent = payload.get("parent_thread_id")
    return "subagent" if isinstance(parent, str) and parent.strip() else None


def _codex_runs(home: Path, now: float) -> tuple[int, int, int]:
    """``(automations, sub-agents, rollouts scanned)`` among Codex rollouts written in the last 7 days."""
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return 0, 0, 0
    cutoff = now - AUTOMATION_SCAN_DAYS * 86400
    recent: list[tuple[float, Path]] = []
    try:
        for path in root.rglob("rollout-*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                recent.append((mtime, path))
    except OSError:
        return 0, 0, 0
    recent.sort(reverse=True)
    automations = subagents = 0
    for _, path in recent[:AUTOMATION_SCAN_MAX_FILES]:
        kind = thread_kind(read_first_line(path))
        automations += kind == "automation"
        subagents += kind == "subagent"
    return automations, subagents, min(len(recent), AUTOMATION_SCAN_MAX_FILES)


def _skipped_logged(home: Path) -> int:
    count = 0
    for name in ("relay.log", "relay.log.1"):
        with contextlib.suppress(OSError):
            text = (outbox.relay_dir(home) / name).read_text(encoding="utf-8", errors="replace")
            count += len(re.findall(r"skipped (?:brief|close): codex automation session", text))
    return count


def _repo(cwd: Path) -> tuple[str, str | None] | None:
    """``(repository name, branch)`` from the ``.git`` files above ``cwd`` (no git process)."""
    try:
        here = cwd.resolve()
    except OSError:
        return None
    for folder in (here, *here.parents):
        dot = folder / ".git"
        try:
            if dot.is_dir():
                head_file = dot / "HEAD"
            elif dot.is_file():
                gitdir = dot.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
                head_file = (folder / gitdir / "HEAD") if not os.path.isabs(gitdir) else Path(gitdir) / "HEAD"
            else:
                continue
            head = head_file.read_text(encoding="utf-8").strip()
        except OSError:
            return (folder.name, None)
        branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else "(detached)"
        return (folder.name, clean_text(branch, 40))
    return None


def _namespace(config: RelayConfig, environ: Mapping[str, str], home: Path) -> tuple[str, str] | None:
    aliases = parse_project_aliases(config.project_aliases)
    relay_project = environ.get("REMEMBRA_RELAY_PROJECT")
    if relay_project and relay_project.strip():
        project = normalize_project_id(relay_project, aliases)
        if project and project != "default":
            return project, "REMEMBRA_RELAY_PROJECT"
    if config.project and config.project.strip():
        project = normalize_project_id(config.project, aliases)
        if project and project != "default":
            where = "REMEMBRA_PROJECT" if environ.get("REMEMBRA_PROJECT") else _source(config, home)
            return project, where
    return None


# ---------------------------------------------------------------------------
# Server reads
# ---------------------------------------------------------------------------


class _Budget:
    def __init__(self, max_gets: int, seconds: float) -> None:
        self.left = max_gets
        self.deadline = time.monotonic() + seconds


@dataclass
class _Answer:
    status: int | None
    body: Any
    text: str
    headers: dict[str, str]
    ms: int
    error: str | None


class ServerReader:
    """GETs to the allow-listed trail paths only, with the user's own key; bounded in count and time."""

    def __init__(self, url: str, api_key: str, budget: _Budget, transport: httpx.BaseTransport | None = None) -> None:
        self.url = url
        self._key = api_key
        self._budget = budget
        self._transport = transport

    def get(self, path: str, params: dict[str, Any]) -> _Answer:
        if path not in ALLOWED_GETS:
            raise ValueError(f"marshal never requests {path}")
        if self._budget.left <= 0:
            raise RuntimeError("the doctor's read budget is spent")
        self._budget.left -= 1
        remaining = self._budget.deadline - time.monotonic()
        started = time.monotonic()
        if remaining <= 0.2:
            return _Answer(None, None, "", {}, 0, "TimeoutError: the doctor's time for server reads ran out")
        timeout = min(HTTP_TIMEOUT_SECONDS, remaining)
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                headers = {
                    "User-Agent": f"remembra-doctor/{_version()}",
                    "Accept": "application/json",
                    "X-API-Key": self._key,
                }
                with httpx.Client(
                    base_url=self.url,
                    headers=headers,
                    timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
                    transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    box["response"] = client.request("GET", path, params=params)
            except BaseException as e:  # handed to the caller's thread
                box["error"] = e

        worker = threading.Thread(target=run, name="remembra-doctor-http", daemon=True)
        worker.start()
        worker.join(timeout + 0.5)
        ms = int((time.monotonic() - started) * 1000)
        if worker.is_alive():
            return _Answer(None, None, "", {}, ms, f"TimeoutError: no complete answer within {timeout:.0f}s")
        if "error" in box:
            e = box["error"]
            return _Answer(None, None, "", {}, ms, f"{e.__class__.__name__}: {clean_text(str(e), 120)}")
        response: httpx.Response = box["response"]
        try:
            body = response.json()
        except Exception:
            body = None
        return _Answer(response.status_code, body, response.text[:4000], dict(response.headers), ms, None)


def _detail(answer: _Answer) -> str:
    if isinstance(answer.body, dict) and answer.body.get("detail") is not None:
        return clean_text(str(answer.body["detail"]), 120)
    return clean_text(answer.text, 80)


def _key_state(answer: _Answer) -> tuple[str, str | None, str | None]:
    """``(state, error, ray id)`` for the key check answer."""
    if answer.error is not None:
        return "unchecked", f"server unreachable ({answer.error})", None
    assert answer.status is not None
    if answer.status < 400:
        return "accepted", None, None
    blocked, ray = is_firewall_block(answer.status, answer.text, answer.headers)
    if blocked:
        return "firewall", "HTTP 403 from the server's firewall, not from Remembra", ray
    if answer.status == 401:
        return "rejected", f"HTTP 401: {_detail(answer)}", None
    if answer.status == 403:
        return "refused", f"HTTP 403: {_detail(answer)}", None
    return "unchecked", f"HTTP {answer.status}: {_detail(answer)}", None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def source_label(source: str, home: Path) -> str:
    """A config source (``credentials:/home/me/.remembra/credentials``) as a path with ``~``; ``env`` and ``none`` as they are."""
    return tilde(source.partition(":")[2] or source, home)


def _source(config: RelayConfig, home: Path) -> str:
    return source_label(config.source, home)


def _summary(body: Any) -> tuple[dict[str, int], dict[str, int], dict[str, float], dict[str, int]]:
    """Per agent: entries in the 7 days, handoffs in the 7 days, the newest entry's time, handoffs all-time."""
    entries: dict[str, int] = {}
    handoffs: dict[str, int] = {}
    last: dict[str, float] = {}
    all_time: dict[str, int] = {}
    agents = body.get("agents") if isinstance(body, dict) else None
    for item in agents if isinstance(agents, list) else []:
        if not isinstance(item, dict):
            continue
        label = agent_label(item.get("agent_id"))
        if not label:
            continue
        raw_daily = item.get("daily")
        daily: list[Any] = raw_daily if isinstance(raw_daily, list) else []
        entries[label] = entries.get(label, 0) + sum(_count(n) for n in daily)
        handoffs[label] = handoffs.get(label, 0) + _count(item.get("sessions_7d"))
        all_time[label] = all_time.get(label, 0) + _count(item.get("handoffs"))
        at = parse_time(item.get("last_active"))
        if at is not None:
            last[label] = max(at, last.get(label, 0.0))
    return entries, handoffs, last, all_time


def _trail(items: list[Any]) -> dict[str, Any]:
    last_entry: dict[str, tuple[str, float]] = {}
    pickups_by: dict[str, int] = {}
    last_pickup: dict[str, float] = {}
    handoffs: list[tuple[str, float, list[tuple[str, float]]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        agent = agent_label(item.get("agent_id"))
        kind = item.get("memory_type")
        at = parse_time(item.get("created_at"))
        if not agent or kind not in ("handoff", "checkpoint") or at is None:
            continue
        if agent not in last_entry or at > last_entry[agent][1]:
            last_entry[agent] = (str(kind), at)
        picks: list[tuple[str, float]] = []
        raw_picks = item.get("picked_up_by")
        for pick in raw_picks if isinstance(raw_picks, list) else []:
            reader = agent_label(pick.get("agent_id")) if isinstance(pick, dict) else None
            when = parse_time(pick.get("picked_up_at")) if isinstance(pick, dict) else None
            if reader and when is not None:
                picks.append((reader, when))
                last_pickup[reader] = max(when, last_pickup.get(reader, 0.0))
        for reader in dict.fromkeys(r for r, _ in picks):  # one per entry, whatever the reader sessions (as the slip counts)
            pickups_by[reader] = pickups_by.get(reader, 0) + 1
        if kind == "handoff":
            handoffs.append((agent, at, picks))
    ordered = sorted(handoffs, key=lambda h: h[1], reverse=True)
    return {
        "last_entry": last_entry,
        "pickups_by": pickups_by,
        "last_pickup": last_pickup,
        "handoffs": tuple((a, at, tuple(sorted(p, key=lambda x: x[1]))) for a, at, p in ordered),
    }


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def automation_skip_supported() -> bool:
    """True when this install's relay skips Codex automation and sub-agent sessions (``remembra.relay.background``)."""
    return importlib.util.find_spec("remembra.relay.background") is not None


def hooks_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment without what a Remembra MCP server's own entry injects (the hooks never see those)."""
    return {k: v for k, v in environ.items() if k not in _MCP_ENV_KEYS}


def _key_digest(config: RelayConfig) -> str:
    return hashlib.sha256(f"{config.api_key}\x1f{config.url}".encode()).hexdigest() if config.api_key else "none"


def collect(
    home: Path,
    agents: list[str] | None = None,
    check_server: bool = True,
    now: float | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    which: Callable[[str], str | None] = shutil.which,
    cwd: Path | None = None,
) -> Signals:
    """Everything the rules look at. Reads only; see the module docstring for the list."""
    env = dict(os.environ if environ is None else environ)
    home = Path(home)
    now = time.time() if now is None else now
    wanted = tuple(a.lower() for a in (agents or []))
    reads: list[Read] = []
    unchecked: list[str] = []

    primary = load_config(environ=env, home=home)
    status = outbox.read_status(home)
    raw_agents, raw_keys = status.get("agents"), status.get("keys")
    status_agents: dict[str, Any] = raw_agents if isinstance(raw_agents, dict) else {}
    recorded_keys: dict[str, Any] = raw_keys if isinstance(raw_keys, dict) else {}

    # The key each agent's hooks use (a Codex hook prefers Codex's config, a Claude hook Claude's).
    per_agent_config: dict[str, RelayConfig] = {}
    for name, adapter in REGISTRY.items():
        prefer = adapter.spec.config_source
        per_agent_config[name] = load_config(environ=env, home=home, prefer=prefer) if prefer else primary

    agent_signals = {
        name: _agent_signals(
            name,
            adapter,
            home,
            which,
            env,
            status_agents,
            _source(per_agent_config[name], home),
        )
        for name, adapter in REGISTRY.items()
    }

    # Distinct keys, primary first.
    groups: dict[str, tuple[RelayConfig, list[str]]] = {_key_digest(primary): (primary, [])}
    for name, config in per_agent_config.items():
        groups.setdefault(_key_digest(config), (config, []))[1].append(name)

    entries, unreadable = _outbox(home, env)
    queued = [e for e in entries if not e.held]
    held = [e for e in entries if e.held]
    reads.append(
        Read(
            "outbox",
            f"{len(queued)} waiting · {len(held)} held" + (f" · {len(unreadable)} unreadable" if unreadable else ""),
            ok=not entries and not unreadable,
        )
    )

    budget = _Budget(MAX_GETS, TOTAL_HTTP_SECONDS)
    keys: list[KeyCheck] = []
    server: ServerSignals | None = None
    primary_reader: ServerReader | None = None
    summary_body: Any = None
    for index, (digest, (config, names)) in enumerate(groups.items()):
        source = _source(config, home)
        host = display_url(config.url)
        recorded = recorded_keys.get(config.source) if isinstance(recorded_keys.get(config.source), dict) else None
        if not config.api_key:
            keys.append(KeyCheck(source=source, url=host, state="missing", agents=tuple(names), primary=index == 0))
            if index == 0:
                checked = "REMEMBRA_API_KEY, ~/.claude.json, ~/.codex/config.toml, ~/.remembra/credentials"
                reads.append(Read("key", f"none found: {checked}", ok=False))
            continue
        if not check_server:
            keys.append(
                KeyCheck(source=source, url=host, state="skipped", agents=tuple(names), recorded=recorded, primary=index == 0)
            )
            last = f" · hooks last got: {recorded.get('state')}" if recorded else ""
            reads.append(Read("key", f"from {source} · not asked (--no-server){last}"))
            continue
        if digest != _key_digest(primary) and budget.left <= 2:
            keys.append(
                KeyCheck(
                    source=source,
                    url=host,
                    state="unchecked",
                    agents=tuple(names),
                    error="not checked (read budget)",
                    recorded=recorded,
                )
            )
            continue
        reader = ServerReader(config.url, config.api_key, budget, transport)
        answer = reader.get("/api/v1/trail/summary", {"days": 7 if index == 0 else 1})
        state, error, ray = _key_state(answer)
        keys.append(
            KeyCheck(
                source=source,
                url=host,
                state=state,
                agents=tuple(names),
                http_status=answer.status,
                error=error,
                ray_id=ray,
                recorded=recorded,
                primary=index == 0,
            )
        )
        shown_host = host.split("://", 1)[-1]
        verdict = {
            "accepted": f"accepted by {shown_host}",
            "rejected": f"REJECTED by {shown_host} (HTTP 401)",
            "refused": f"refused by {shown_host} (HTTP 403)",
            "firewall": f"blocked by {shown_host}'s firewall (HTTP 403, not Remembra)",
        }.get(state, f"not checked: {error}")
        reads.append(Read("key", f"{verdict} · from {source}", answer.ms, ok=state == "accepted"))
        if index == 0 and state == "accepted":
            primary_reader = reader
            summary_body = answer.body

    if primary_reader is not None:
        entries_7d, handoffs_7d, last_active, handoffs_all = _summary(summary_body)
        seen = sorted(entries_7d, key=lambda a: (-entries_7d[a], a))
        parts = [f"{a} {entries_7d[a]} entr{'y' if entries_7d[a] == 1 else 'ies'}" for a in seen[:4]]
        reads.append(Read("server", "7d: " + (" · ".join(parts) if parts else "no handoffs or checkpoints")))
        trail_answer = primary_reader.get("/api/v1/trail", {"limit": TRAIL_WINDOW})
        items: list[Any] = []
        trail_ok = trail_answer.status is not None and trail_answer.status < 400 and isinstance(trail_answer.body, dict)
        if trail_ok:
            raw_items = trail_answer.body.get("items")
            items = raw_items if isinstance(raw_items, list) else []
        trail = _trail(items)
        if trail_ok:
            pickups = sum(trail["pickups_by"].values())
            reads.append(Read("trail", f"last {TRAIL_WINDOW} entries: {len(items)} read · {pickups} pickups", trail_answer.ms))
        else:
            why = trail_answer.error or f"HTTP {trail_answer.status}: {_detail(trail_answer)}"
            reads.append(Read("trail", f"couldn't read ({why})", trail_answer.ms, ok=False))
            unchecked.append("pickups and last entries (the trail could not be read)")
        # An agent whose newest entry is older than the window: read its own last entries.
        if trail_ok:
            want_detail = [n for n in (wanted or tuple(agent_signals)) if n in last_active and n not in trail["last_entry"]]
            for name in want_detail[: max(0, budget.left)]:
                answer = primary_reader.get("/api/v1/trail", {"agent_id": name, "limit": 5})
                if answer.status is not None and answer.status < 400 and isinstance(answer.body, dict):
                    raw = answer.body.get("items")
                    more = _trail(raw if isinstance(raw, list) else [])
                    if name in more["last_entry"]:
                        trail["last_entry"][name] = more["last_entry"][name]
                    reads.append(Read("trail", f"{name}: its last 5 entries read", answer.ms))
                else:
                    reads.append(Read("trail", f"{name}: couldn't read its entries", answer.ms, ok=False))
        server = ServerSignals(
            url=display_url(primary_reader.url),
            entries_7d=entries_7d,
            handoffs_7d=handoffs_7d,
            last_active=last_active,
            trail_read=trail_ok,
            last_entry=trail["last_entry"],
            pickups_by=trail["pickups_by"],
            last_pickup=trail["last_pickup"],
            handoffs=trail["handoffs"],
            handoffs_all=handoffs_all,
        )
    elif check_server and primary.api_key:
        unchecked.append("your trail (the server did not accept a read)")
    elif not check_server:
        unchecked.append("your trail (--no-server)")

    # Codex: trust and automation runs.
    codex_agent = agent_signals.get("codex")
    trust = (
        codex_hooks.read_trust(home, hooks_path=config_file(REGISTRY["codex"], home))
        if codex_agent and codex_agent.any_hooks
        else None
    )
    if trust is not None and trust.config_state == "unreadable":
        unchecked.append(f"Codex hook trust ({tilde(trust.config_path, home)} could not be read)")
    automations, subagents, scanned = _codex_runs(home, now)
    codex = CodexSignals(
        trust=trust,
        automations_7d=automations,
        subagents_7d=subagents,
        rollouts_scanned=scanned,
        skip_supported=automation_skip_supported(),
        include_automations=(env.get("REMEMBRA_RELAY_INCLUDE_AUTOMATIONS") or "").strip().lower() in _TRUE,
        skipped_logged=_skipped_logged(home),
    )
    if agent_signals["kimi"].detected:
        unchecked.append("Kimi's MCP server entry (its config is not read here)")

    # Only agents on this machine (or already wired) count: a leftover entry for an absent agent reads nothing.
    mcp_projects = {
        name: sig.mcp_project
        for name, sig in agent_signals.items()
        if sig.mcp == "configured" and sig.mcp_project and (sig.detected or sig.any_hooks)
    }
    return Signals(
        version=_version(),
        generated_at=datetime.fromtimestamp(now, UTC).isoformat(),
        now=now,
        home=home,
        repo=_repo(cwd or Path(os.getcwd())),
        config=primary.redacted() | {"url": display_url(primary.url), "source": _source(primary, home)},
        wanted=wanted,
        keys=tuple(keys),
        outbox=tuple(entries),
        outbox_unreadable=tuple(unreadable),
        status_agents=status_agents,
        agents=agent_signals,
        codex=codex,
        close_log=_close_log(home),
        namespace=_namespace(primary, env, home),
        mcp_projects={k: v for k, v in mcp_projects.items() if v},
        server=server,
        check_server=check_server,
        reads=tuple(reads),
        unchecked=tuple(unchecked),
        server_url=display_url(primary.url),
    )
