"""Codex threads that get no brief and leave no handoff: automations and sub-agents.

Codex Desktop runs scheduled automations (dozens a day on a busy machine) and
spawns sub-agent threads. With the relay hooks trusted, an automation run that
gets its own thread (a cron automation, ``thread_source: "automation"``) runs
SessionStart (``brief``) and SessionEnd (``close``): the trail fills with
automation handoffs that bury the sessions people work in, the next brief
points at an automation run, and each automation gets a project brief in its
prompt. A sub-agent thread runs hooks too: under ``codex exec``
0.155.0-alpha.16.4 its UserPromptSubmit hook fires with the PARENT's
``session_id``, the sub-agent's own thread id as ``agent_id`` and the
sub-agent's own rollout as ``transcript_path`` (see
tests/test_relay_codex_live.py). A heartbeat automation is NOT skipped: it
posts its turns into an existing thread (one someone started, or a voice
chat), whose rollout starts the way that thread started, so any hook a
heartbeat turn runs is handled as that thread's own (the thread holds the
person's own work; telling a heartbeat turn apart would need a per-turn
signal the first line cannot give).

Codex's hook payload does not say what kind of thread it is. The rollout it
names (``transcript_path``) does: its first line is a ``session_meta`` record
whose payload has ``thread_source`` (``"user"``, ``"automation"``,
``"subagent"``, ``"voice_chat"``, ``"agent_created_thread"``, ...), ``source``
(a string such as ``"vscode"`` or ``"exec"``, or ``{"subagent": {...}}`` for a
sub-agent thread) and, for a sub-agent, ``parent_thread_id``. Only that first
line is read, and at most :data:`FIRST_LINE_MAX_BYTES` of it (Codex's is about
20 KB: it carries the built-in instructions). The path comes from the agent's
own hook payload and is not limited to one directory (``CODEX_HOME`` can be
anywhere), so only a regular file is read: anything else is refused before it
is opened (a device named there never reaches its driver), and the open is
non-blocking, so a FIFO swapped in after that check cannot stall the hook.
Anything unexpected (no file, not a regular file, a first line that is too
long, not JSON or not a ``session_meta``) means the session is not skipped;
nothing here raises.

``REMEMBRA_RELAY_INCLUDE_AUTOMATIONS=1`` keeps automation sessions (brief and
handoff) for people who want them. Sub-agent threads are skipped either way:
they run inside a session of their own parent, whose handoff covers the work.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from remembra.relay import outbox
from remembra.relay.facts import CODEX_ROLLOUT

if TYPE_CHECKING:
    from remembra.relay.adapters.base import Adapter

AUTOMATION = "automation"
SUBAGENT = "subagent"
INCLUDE_AUTOMATIONS_ENV = "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS"
FIRST_LINE_MAX_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
_TRUE = frozenset({"1", "true", "yes", "on"})


def read_first_line(path: str | os.PathLike[str], max_bytes: int = FIRST_LINE_MAX_BYTES) -> bytes | None:
    """The first line of a regular file, without its newline; None when there is none to read.

    None for a missing or unreadable path, anything but a regular file, an
    empty or blank first line, and a first line longer than ``max_bytes`` (at
    most ``max_bytes + 1`` bytes are read). Never raises.

    Anything but a regular file is refused before it is opened: opening a
    device reaches its driver (a serial port resets the board on it, a
    watchdog arms). The open is non-blocking and the opened file is checked
    again, so a FIFO or device swapped in between is still neither waited on
    nor read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # follows symlinks, as the open does
            return None
        fd = os.open(path, flags)
    except (OSError, ValueError, TypeError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # replaced between the stat and the open
            return None
        line = bytearray()
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, max_bytes + 1 - len(line)))
            if not chunk:
                break  # end of file: the file is a single line
            newline = chunk.find(b"\n")
            if newline >= 0:
                line += chunk[:newline]
                break
            line += chunk
            if len(line) > max_bytes:
                return None
        return bytes(line) if line.strip() else None
    except OSError:
        return None
    finally:
        os.close(fd)


def read_session_meta(path: str | os.PathLike[str], max_bytes: int = FIRST_LINE_MAX_BYTES) -> dict[str, Any] | None:
    """The payload of a Codex rollout's first record when it is a ``session_meta``; None otherwise. Never raises."""
    line = read_first_line(path, max_bytes)
    if line is None:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested too deep to parse
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def background_kind(meta: Mapping[str, Any]) -> str | None:
    """``"automation"`` or ``"subagent"`` for a thread nobody is typing in, from its ``session_meta`` payload; else None.

    A sub-agent is ``thread_source: "subagent"``, a ``source`` object with a
    ``subagent`` key, or any non-empty ``parent_thread_id``. Every other
    thread (``"user"``, ``"voice_chat"``, ``"agent_created_thread"``, a
    missing field, a kind Codex adds later) is not skipped.
    """
    thread_source = meta.get("thread_source")
    if isinstance(thread_source, str) and thread_source.strip().lower() in (AUTOMATION, SUBAGENT):
        return thread_source.strip().lower()
    source = meta.get("source")
    if isinstance(source, Mapping) and SUBAGENT in source:
        return SUBAGENT
    parent = meta.get("parent_thread_id")
    if isinstance(parent, str) and parent.strip():
        return SUBAGENT
    return None


def session_skip_reason(payload: Mapping[str, Any], transcript: str | os.PathLike[str] | None = None) -> str | None:
    """Why the session of this Codex hook gets no brief and no handoff: ``"automation"``, ``"subagent"`` or None.

    Reads the first line of the rollout the payload names (``transcript_path``,
    or ``transcript`` when given). Never raises: a payload without a usable
    path, or a rollout that cannot be read, means None (the session goes on).
    """
    try:
        path = transcript if transcript is not None else payload.get("transcript_path")
        if not isinstance(path, str | os.PathLike) or not str(path).strip():
            return None
        meta = read_session_meta(Path(path).expanduser())
        return background_kind(meta) if meta is not None else None
    except Exception:
        return None


def include_automations(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``REMEMBRA_RELAY_INCLUDE_AUTOMATIONS`` is set to 1 / true / yes / on."""
    env = os.environ if environ is None else environ
    return (env.get(INCLUDE_AUTOMATIONS_ENV) or "").strip().lower() in _TRUE


def skip_hook_session(
    adapter: Adapter,
    payload: Mapping[str, Any],
    command: str,
    home: Path,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The reason a ``<command> --hook`` run does nothing for this session, or None when it goes on.

    Applies to adapters whose transcripts are Codex rollouts. A skipped run
    writes one line to relay.log and nothing else: no output, no request, no
    queued handoff. The line names the session (``skipped brief: codex
    automation session <id>``), or, when the payload carries an ``agent_id``
    other than its ``session_id`` (a sub-agent's hook carries its parent's
    session id), the thread first: ``skipped brief: codex subagent thread
    <agent_id> of session <session_id>``. Automations go on when
    :func:`include_automations`. Never raises.
    """
    try:
        if adapter.spec.transcript_format != CODEX_ROLLOUT:
            return None
        fields = adapter.spec.payload.extract(dict(payload), dict(environ) if environ is not None else None)
        reason = session_skip_reason(payload, fields.get("transcript"))
        if reason is None or (reason == AUTOMATION and include_automations(environ)):
            return None
        session = _log_id(fields.get("session_id")) or "without an id"
        thread = _log_id(payload.get("agent_id"))
        who = f"thread {thread} of session {session}" if thread and thread != session else f"session {session}"
        outbox.log(home, f"skipped {command}: {adapter.spec.name} {reason} {who}")
        return reason
    except Exception:
        return None


def _log_id(value: object) -> str | None:
    """An id from a hook payload as relay.log prints it: stripped, at most 40 characters; None when absent."""
    if not isinstance(value, str):
        return None
    return value.strip()[:40] or None
