"""Local outbox, log and status for ``remembra-relay``: a handoff is never lost silently.

When ``close`` cannot deliver a handoff (server down, timeout, 5xx, 429, a
rejected key, no key yet) the request body is queued in
``~/.remembra/relay/outbox/<agent>-<session>.json`` and a line goes to
``~/.remembra/relay/relay.log``. Every later ``brief`` and ``close`` sends the
queue again, and so does ``remembra-relay drain``: a detached process a close
starts when it queues on a failure that waiting can fix, and a crew session's
start and end start when something is queued (:func:`kick_drain`), so a queued
handoff does not wait for the next relay hook. The server keeps one current handoff per (agent, session): a
repeat close supersedes the previous one, an identical one changes nothing,
so sending an entry twice (a crash between the send and the delete) is safe.

Files:

* Entries, the log and the status file are written 0600 in 0700 directories,
  entries and status atomically (temp file, fsync, rename), so a crash leaves
  either the old or the new file, never a torn one.
* The queue is bounded (``MAX_ENTRIES`` entries of at most ``MAX_ENTRY_BYTES``,
  none older than ``MAX_AGE_SECONDS``); what is dropped is logged.
* An entry holds the close body after the same secret redaction the server
  applies to a handoff, the server URL (no credentials) and which config
  source supplied the key. Never the key itself.
* A sender claims an entry by renaming it, so two hooks never send the same
  file at once; a newer close of the same session replaces the queued one.

Standard library only (plus the relay's own redaction): it runs inside hooks.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from remembra.relay.handoff import redact

if TYPE_CHECKING:
    from remembra.relay.config import RelayConfig

MAX_ENTRIES = 50
MAX_ENTRY_BYTES = 256 * 1024
MAX_AGE_SECONDS = 14 * 86400
LOG_MAX_BYTES = 256 * 1024
LOG_GENERATIONS = 3  # relay.log.1 (newest) to relay.log.3 are kept next to relay.log
RECENT_FAILURES = 10  # failures kept per agent in status.json, oldest first
STALE_CLAIM_SECONDS = 120
ENTRY_VERSION = 1
# ``config_source`` of an entry the MCP server queued with its own key (remembra.mcp.close_queue). No
# config file is named by it, so a relay that does not know the value holds the entry instead of
# sending it with whatever key it finds; one that does sends it only with the key marked in ``key_fp``.
MCP_SOURCE = "mcp"
CLAIM_MARK = ".sending-"

# HTTP answers worth retrying later; anything else (400, 404, 413, 422) will
# not get better by sending the same body again. A 403 is kept but only holds
# back its own entry (``remembra.relay.cli.replay_outbox``), and it is not sent
# again by every brief and close: see :func:`refusal_hold`.
RETRY_STATUS = frozenset({401, 403, 408, 409, 425, 429})

# A refused (403) entry waits this long before its next try: after the first
# refusal, the second, and every one from the third on. The owner can widen a
# key's projects in the dashboard, so it is tried again, but rarely.
REFUSAL_BACKOFF_SECONDS = (3600, 6 * 3600, 86400)
# The server's answer when an agent-scoped key writes as another agent
# (``remembra.auth.middleware.enforce_agent_scope``). A key's agent cannot be
# changed, so that key will never be allowed to send the entry.
AGENT_SCOPE_REFUSAL = "scoped to agent"


def is_retryable_status(status: int) -> bool:
    return status in RETRY_STATUS or status >= 500


def route_missing(status: int, body: Any) -> bool:
    """A 404 from a server that has no such route (the framework's bare ``{"detail": "Not Found"}``).

    That is an older server build, for example an API rolled back to a release
    without ``/session/close``: the handoff is queued and sent once the server
    is rolled forward. A 404 the route itself answers carries its own detail
    and is not retried.
    """
    return status == 404 and isinstance(body, dict) and body.get("detail") == "Not Found"


def is_retryable_response(status: int, body: Any) -> bool:
    """:func:`is_retryable_status`, plus a 404 for a route the server does not have (:func:`route_missing`)."""
    return is_retryable_status(status) or route_missing(status, body)


def relay_dir(home: Path) -> Path:
    return home / ".remembra" / "relay"


def outbox_dir(home: Path) -> Path:
    return relay_dir(home) / "outbox"


def log_path(home: Path) -> Path:
    return relay_dir(home) / "relay.log"


def status_path(home: Path) -> Path:
    return relay_dir(home) / "status.json"


def clean_url(url: str | None) -> str | None:
    """``url`` without user:password, query or fragment, trailing slash removed."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host.lower(), parts.path.rstrip("/"), "", "")) or None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` (0600) so a crash leaves the old or the new file."""
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def log_files(home: Path) -> list[Path]:
    """relay.log and its rotated generations, newest first (whether or not they exist)."""
    path = log_path(home)
    return [path, *(path.with_name(f"{path.name}.{n}") for n in range(1, LOG_GENERATIONS + 1))]


def log(home: Path, message: str) -> None:
    """Append one line to relay.log (0600). Past ``LOG_MAX_BYTES`` it becomes relay.log.1 and the older
    generations move up; ``LOG_GENERATIONS`` are kept. Never raises."""
    try:
        path = log_path(home)
        _ensure_dir(path.parent)
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX_BYTES:
                files = log_files(home)
                for older, newer in zip(reversed(files), reversed(files[:-1]), strict=False):
                    with contextlib.suppress(OSError):
                        os.replace(newer, older)
        line = " ".join(str(redact(message)).split())
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} pid={os.getpid()} {line}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Outbox entries
# ---------------------------------------------------------------------------

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, limit: int) -> str:
    return _SAFE_RE.sub("_", value).strip("._-")[:limit] or "x"


def entry_path(home: Path, agent_id: str, session_id: str) -> Path:
    """``<agent>-<session>.json``, file-name safe; a digest keeps distinct ids distinct."""
    name = f"{_slug(agent_id, 40)}-{_slug(session_id, 80)}"
    if name != f"{agent_id}-{session_id}":
        name += "-" + hashlib.sha256(f"{agent_id}\x1f{session_id}".encode()).hexdigest()[:10]
    return outbox_dir(home) / f"{name}.json"


@dataclass
class Entry:
    path: Path
    data: dict[str, Any]

    @property
    def agent_id(self) -> str:
        return str(self.data.get("agent_id") or "")

    @property
    def session_id(self) -> str:
        return str(self.data.get("session_id") or "")

    @property
    def payload(self) -> dict[str, Any]:
        payload = self.data.get("payload")
        return payload if isinstance(payload, dict) else {}

    @property
    def url(self) -> str | None:
        value = self.data.get("url")
        return str(value) if value else None

    @property
    def queued_ts(self) -> float:
        try:
            return float(self.data.get("queued_ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def attempts(self) -> int:
        try:
            return int(self.data.get("attempts") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def refusals(self) -> int:
        """How often the server refused (403) this entry; at least 1 while its last answer is a 403."""
        try:
            count = int(self.data.get("refusals") or 0)
        except (TypeError, ValueError):
            count = 0
        refused = self.data.get("last_status") == 403 and not blocked_by_firewall(self)
        return max(count, 1) if refused else max(count, 0)

    @property
    def last_try_ts(self) -> float | None:
        """When it was last sent: ``last_try_ts``, else ``last_try_at`` (what a 0.17.5 client wrote on a retry),
        else when it was queued (all that client wrote for a close never retried); None when none is recorded."""
        try:
            value = float(self.data.get("last_try_ts") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        try:
            return float(calendar.timegm(time.strptime(str(self.data.get("last_try_at")), "%Y-%m-%dT%H:%M:%SZ")))
        except (TypeError, ValueError, OverflowError):
            return self.queued_ts or None


def firewall_block(text: str | None, headers: Mapping[str, str] | None = None) -> bool:
    """Whether a 403 with this body and these headers was written by a firewall in front of the API, not by Remembra.

    Remembra answers errors in JSON; an HTML page, or Cloudflare's ``cf-ray`` / ``server: cloudflare`` headers,
    come from the proxy: the key was never checked. The same test as ``remembra.marshal.signals.is_firewall_block``
    (kept here because this module runs inside hooks and imports nothing of the doctor).
    """
    lower = (text or "").lstrip()[:4000].lower()
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    cloudflare = "cloudflare" in (hdrs.get("server") or "").lower() or bool(hdrs.get("cf-ray")) or "cloudflare" in lower
    html = lower.startswith("<!doctype html") or lower.startswith("<html") or "<head" in lower[:600]
    return cloudflare or html


def blocked_by_firewall(entry: Entry) -> bool:
    """Whether the entry's last answer was a 403 from a firewall (:func:`firewall_block`), not Remembra's refusal.

    ``refused_by`` says so when this client recorded it; an entry without it (a 0.17.5 client wrote it) is
    judged by the stored error text, which starts with the page the proxy sent.
    """
    if entry.data.get("last_status") != 403:
        return False
    by = entry.data.get("refused_by")
    if by:
        return bool(by == "firewall")
    error = str(entry.data.get("last_error") or "")
    return firewall_block(error.split(":", 1)[1] if error.startswith("HTTP 403:") else error)


def refusal_hold(entry: Entry, now: float | None = None) -> tuple[str, str] | None:
    """Why a refused (403) entry is not sent right now: ``(kind, reason)``, or None when it may go.

    ``("agent", ...)``: the key is scoped to another agent than the one the handoff is from. That key can
    never send it, so it is never sent on its own again (``remembra-relay outbox send`` or ``drop`` decide).
    ``("backoff", ...)``: any other refusal (the key may not write to that project) waits
    ``REFUSAL_BACKOFF_SECONDS`` after each refusal instead of going out with every brief and close.
    An entry with no time recorded at all is not held by the backoff. A 403 from a firewall in front of the
    API (:func:`blocked_by_firewall`) is no refusal: the key was never checked, so it is retried like an
    unreachable server.
    """
    if entry.data.get("last_status") != 403 or blocked_by_firewall(entry):
        return None
    error = str(entry.data.get("last_error") or "")
    if AGENT_SCOPE_REFUSAL in error.lower():
        return "agent", (
            f"the server refused it: the key it was sent with writes as another agent than {entry.agent_id or 'its own'}"
            " and that cannot be changed; it is never sent on its own again (`remembra-relay outbox drop` removes it,"
            " `remembra-relay outbox send --source` sends it with another key)"
        )
    tried = entry.last_try_ts
    if tried is None:
        return None
    wait = REFUSAL_BACKOFF_SECONDS[min(entry.refusals, len(REFUSAL_BACKOFF_SECONDS)) - 1]
    left = tried + wait - (time.time() if now is None else now)
    if left <= 0:
        return None
    again = f"{round(left / 3600)}h" if left >= 5400 else f"{max(1, round(left / 60))}m"
    return "backoff", (
        f"the server refused it {entry.refusals} time{'s' if entry.refusals != 1 else ''}; it is tried again in about"
        f" {again} (give the key access in the dashboard, then `remembra-relay outbox send` sends it now)"
    )


def _redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The close body with the redaction the server applies to facts, summary and end reason."""
    out = dict(payload)
    for key in ("facts", "summary", "end_reason"):
        if key in out and out[key] is not None:
            out[key] = redact(out[key])
    return out


def _shrink(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep an oversized body under the cap: long lists and strings in the facts are cut."""

    def cut(value: Any) -> Any:
        if isinstance(value, str):
            return value[:2000]
        if isinstance(value, list):
            return [cut(v) for v in value[:40]]
        if isinstance(value, dict):
            return {k: cut(v) for k, v in value.items()}
        return value

    out = dict(payload)
    facts = out.get("facts")
    if isinstance(facts, dict):
        out["facts"] = {**cut(facts), "incomplete": sorted({*(facts.get("incomplete") or []), "outbox-trimmed"})}
    return out


def key_mark(api_key: str | None) -> str | None:
    """A short one-way mark of an API key: it tells "the same key" from "another key" and is never the key."""
    if not api_key:
        return None
    return hashlib.sha256(b"remembra-relay-outbox:" + api_key.encode()).hexdigest()[:16]


def enqueue(
    home: Path,
    payload: dict[str, Any],
    *,
    url: str | None,
    config_source: str | None,
    error: str,
    http_status: int | None = None,
    api_key: str | None = None,
    extra: Mapping[str, Any] | None = None,
    firewall: bool = False,
) -> Path | None:
    """Queue a close body that could not be delivered. Returns the entry path, or None when it was not kept.

    ``api_key`` is the key the close was tried with. It is never stored: when the server refused the close
    (403), the entry keeps that key's :func:`key_mark` as ``refused_key_fp`` until it is delivered or dropped.
    ``extra`` adds fields of the queuing program to the entry (the MCP server's ``key_fp``, see
    :mod:`remembra.mcp.close_queue`); it never replaces a field written here.
    ``firewall`` says the 403 came from a firewall in front of the API (:func:`firewall_block`): no key was
    refused, so none is recorded and it does not count as a refusal.
    """
    agent_id = str(payload.get("agent_id") or "unknown-agent")
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        log(home, f"outbox: not queued, no session id (agent {agent_id})")
        return None
    refused = http_status == 403 and not firewall
    try:
        path = entry_path(home, agent_id, session_id)
        previous = read_entry(path)
        data: dict[str, Any] = {
            **{str(k): v for k, v in (extra or {}).items()},
            "v": ENTRY_VERSION,
            "agent_id": agent_id,
            "session_id": session_id,
            "queued_at": _now_iso(),
            "queued_ts": time.time(),
            "attempts": (previous.attempts if previous else 0) + 1,
            "url": clean_url(url),
            "config_source": config_source,
            "refused_key_fp": key_mark(api_key) if refused else (previous.data.get("refused_key_fp") if previous else None),
            "refused_by": ("firewall" if firewall else "server") if http_status == 403 else None,
            "last_error": str(redact(error))[:300],
            "last_status": http_status,
            "refusals": (previous.refusals if previous else 0) + (1 if refused else 0),
            "last_try_at": _now_iso(),
            "last_try_ts": time.time(),
            "payload": _redacted_payload(payload),
        }
        text = json.dumps(data, default=str)
        if len(text.encode()) > MAX_ENTRY_BYTES:
            data["payload"] = _shrink(data["payload"])
            text = json.dumps(data, default=str)
        if len(text.encode()) > MAX_ENTRY_BYTES:
            log(home, f"outbox: dropped {agent_id} session {session_id[:40]}: body over {MAX_ENTRY_BYTES} bytes")
            return None
        atomic_write_private(path, text)
        log(home, f"outbox: queued {agent_id} session {session_id[:40]} ({error[:200]})")
        prune(home)
        return path
    except OSError as e:
        log(home, f"outbox: could not queue {agent_id} session {session_id[:40]}: {e.__class__.__name__}")
        return None


def read_entry(path: Path) -> Entry | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
        return None
    return Entry(path=path, data=data)


def _restore_stale_claims(directory: Path) -> None:
    """A sender that crashed leaves ``<entry>.sending-…``: put it back (unless a newer entry exists)."""
    now = time.time()
    for claim in directory.glob(f"*.json{CLAIM_MARK}*"):
        try:
            if now - claim.stat().st_mtime < STALE_CLAIM_SECONDS:
                continue
            original = claim.with_name(claim.name.split(CLAIM_MARK, 1)[0])
            if original.exists():
                claim.unlink()
            else:
                os.replace(claim, original)
        except OSError:
            pass


def pending(home: Path) -> list[Entry]:
    """Queued entries, oldest first. Unreadable files are set aside as ``*.corrupt``."""
    directory = outbox_dir(home)
    if not directory.is_dir():
        return []
    _restore_stale_claims(directory)
    entries: list[Entry] = []
    for path in directory.glob("*.json"):
        entry = read_entry(path)
        if entry is None:
            with contextlib.suppress(OSError):
                os.replace(path, path.with_name(path.name + ".corrupt"))
            log(home, f"outbox: set aside unreadable entry {path.name}")
            continue
        entries.append(entry)
    entries.sort(key=lambda e: (e.queued_ts, e.path.name))
    return entries


def prune(home: Path) -> list[str]:
    """Drop entries past ``MAX_AGE_SECONDS`` and the oldest beyond ``MAX_ENTRIES``; each drop is logged."""
    dropped: list[str] = []
    entries = pending(home)
    now = time.time()
    keep: list[Entry] = []
    for entry in entries:
        if entry.queued_ts and now - entry.queued_ts > MAX_AGE_SECONDS:
            dropped.append(entry.path.name)
            log(home, f"outbox: dropped {entry.agent_id} session {entry.session_id[:40]}: older than 14 days")
            with contextlib.suppress(OSError):
                entry.path.unlink()
        else:
            keep.append(entry)
    while len(keep) > MAX_ENTRIES:
        entry = keep.pop(0)
        dropped.append(entry.path.name)
        log(home, f"outbox: dropped {entry.agent_id} session {entry.session_id[:40]}: queue full ({MAX_ENTRIES})")
        with contextlib.suppress(OSError):
            entry.path.unlink()
    return dropped


def claim(entry: Entry) -> Path | None:
    """Take an entry for sending (atomic rename). None when another process has it."""
    target = entry.path.with_name(f"{entry.path.name}{CLAIM_MARK}{os.getpid()}-{os.urandom(3).hex()}")
    try:
        os.replace(entry.path, target)
    except OSError:
        return None
    return target


def finish(
    entry: Entry,
    claimed: Path,
    *,
    sent: bool,
    error: str | None = None,
    http_status: int | None = None,
    api_key: str | None = None,
    firewall: bool = False,
) -> None:
    """After a send: delete the claim when delivered, else put it back (a newer queued close wins).

    A 403 records the :func:`key_mark` of ``api_key``, the key that was refused; a later failure of
    another kind does not clear it. A 403 from a firewall (``firewall``) refused no key: it is recorded as
    ``refused_by: firewall`` and neither the key mark nor the refusal count changes.
    """
    try:
        if sent or entry.path.exists():
            claimed.unlink()
            return
        data = dict(entry.data)
        data["attempts"] = entry.attempts + 1
        if error:
            data["last_error"] = str(redact(error))[:300]
        data["last_status"] = http_status
        data["refused_by"] = ("firewall" if firewall else "server") if http_status == 403 else None
        if http_status == 403 and not firewall:
            data["refused_key_fp"] = key_mark(api_key)
            data["refusals"] = entry.refusals + 1
        data["last_try_at"] = _now_iso()
        data["last_try_ts"] = time.time()
        atomic_write_private(claimed, json.dumps(data, default=str))
        os.replace(claimed, entry.path)
    except OSError:
        pass


def drop(home: Path, entry: Entry, why: str) -> bool:
    """Remove one queued entry on the owner's word (``remembra-relay outbox drop``); logged. False when it is gone."""
    try:
        entry.path.unlink()
    except OSError:
        return False
    log(home, f"outbox: dropped {entry.agent_id} session {entry.session_id[:40]}: {why}")
    return True


def _queued_close_failure(event: Any, dropped: bool) -> bool:
    """Whether a recorded failure is about a QUEUED close: a resend of the queue, or (``dropped``: its entry was
    just removed) the close that queued it. A close the server rejected for good (422) was never queued."""
    if not isinstance(event, dict):
        return False
    command = str(event.get("command") or "")
    if command == "close (queued)":
        return True
    status = event.get("http_status")
    return dropped and command == "close" and (status is None or (isinstance(status, int) and is_retryable_status(status)))


def clear_queued_failure(home: Path, agent_id: str, *, dropped: bool = False) -> bool:
    """Forget ``agent_id``'s recorded queued-close failures once nothing of it is queued (status and doctor list
    them otherwise).

    Only failures about a queued close go (:func:`_queued_close_failure`; ``dropped`` is for the caller that
    just removed the agent's entry): from ``last_failure``, ``by_command`` and ``recent_failures`` alike. The
    newest failure that is left becomes ``last_failure`` again; any other failure is never touched. True when
    something was removed. Never raises.
    """
    try:
        if any(e.agent_id == agent_id for e in pending(home)):
            return False
        with _status_lock(home):
            data = read_status(home)
            slot = (data.get("agents") or {}).get(agent_id)
            if not isinstance(slot, dict):
                return False
            changed = False
            recent = slot.get("recent_failures")
            if isinstance(recent, list):
                kept = [event for event in recent if not _queued_close_failure(event, dropped)]
                changed = len(kept) != len(recent)
                slot["recent_failures"] = kept
            commands = slot.get("by_command")
            for per_command in commands.values() if isinstance(commands, dict) else ():
                if isinstance(per_command, dict) and _queued_close_failure(per_command.get("last_failure"), dropped):
                    del per_command["last_failure"]
                    changed = True
            if _queued_close_failure(slot.get("last_failure"), dropped):
                del slot["last_failure"]
                changed = True
                left = [event for event in slot.get("recent_failures") or [] if isinstance(event, dict)]
                if left:
                    slot["last_failure"] = left[-1]
            if changed:
                atomic_write_private(status_path(home), json.dumps(data, indent=2, default=str))
            return changed
    except Exception:
        return False


def discard(home: Path, agent_id: str, session_id: str) -> bool:
    """Remove the queued close of a session a newer close just delivered (it is superseded)."""
    path = entry_path(home, agent_id, session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    log(home, f"outbox: dropped queued {agent_id} session {session_id[:40]}: a newer close was delivered")
    return True


def replay_config(
    entry: Entry, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[RelayConfig | None, str | None]:
    """The config a queued close may be sent with, or ``(None, why it is held)``.

    The rules are described at ``remembra.relay.cli.replay_config``, which calls this with the process
    environment; :func:`kick_drain` calls it with the environment its drain will run in.
    """
    from remembra.relay.config import load_config, load_config_from_source

    recorded = entry.data.get("config_source")
    agent = entry.agent_id or None
    config: RelayConfig | None
    if recorded and recorded not in ("none", MCP_SOURCE):
        config = load_config_from_source(str(recorded), agent=agent, environ=environ)
        if config is None or not config.api_key:
            return None, f"its key source ({recorded}) has no key now"
    elif recorded == MCP_SOURCE:
        # queued by the MCP server with its own key: the relay may send it when it holds that same key
        config = load_config(agent=agent, environ=environ, home=home)
        queued_with = entry.data.get("key_fp")
        if not queued_with or not config.api_key or key_mark(config.api_key) != queued_with:
            return None, (
                "the MCP server queued it with a key the relay does not have here; the MCP server sends it itself"
                " once the server answers (delete the file to drop it)"
            )
    else:
        config = load_config(agent=agent, environ=environ, home=home)
        if not config.api_key:
            return None, "no API key is configured yet"
    if not entry.url:
        return None, "it was queued without a server; it is only kept, never sent (delete the file to drop it)"
    if clean_url(config.url) != entry.url:
        return None, (
            f"it is for {entry.url}, and the key now points at {clean_url(config.url)}; it is only sent to the"
            " server it was queued for (delete the file to drop it)"
        )
    refused_for = entry.data.get("refused_key_fp")
    if refused_for and key_mark(config.api_key) != refused_for:
        return None, (
            "the server refused it for the key it was sent with, and a different key is configured now; it is only"
            " sent with the key that was refused (give that key access in the dashboard, or delete the file to drop it)"
        )
    if not refused_for and entry.data.get("last_status") == 403 and not blocked_by_firewall(entry):
        return None, (
            "the server refused it, and it was queued before this client recorded which key was refused; it is kept"
            " and never sent on its own (delete the file to drop it)"
        )
    return config, None


def sendable(entry: Entry, environ: Mapping[str, str] | None = None, home: Path | None = None) -> bool:
    """Whether trying again may deliver a queued entry: its key was not rejected (401), :func:`replay_config`
    does not hold it (another server, no key, another key than the refused one) and no refusal holds it
    (:func:`refusal_hold`). What a drain is started for, and what it goes on for."""
    if entry.data.get("last_status") == 401:
        return False
    return replay_config(entry, environ, home)[0] is not None and refusal_hold(entry) is None


# ---------------------------------------------------------------------------
# Drain: send the queue without waiting for the next brief or close
# ---------------------------------------------------------------------------

DRAIN_ENV = "REMEMBRA_RELAY_DRAIN"  # "off", or the waits in seconds ("45,180")
# Seconds a drain waits before each try. Short on purpose: it covers a network that comes back within
# minutes (Wi-Fi, DNS, a timeout); after that the next hook starts another drain.
DRAIN_WAITS_SECONDS = (45.0, 180.0)
DRAIN_MAX_SECONDS = 1800.0  # a drain never lives longer, however often it is asked to go again
# A drain touches its lock every second it waits and before every pass (``remembra.relay.cli.DRAIN_PASS_SECONDS``
# is the longest it goes without). A lock older than this has no drain behind it, whatever its pid says: after a
# restart that pid may belong to another process.
DRAIN_LOCK_STALE_SECONDS = 300.0


def drain_waits(environ: Mapping[str, str] | None = None) -> tuple[float, ...] | None:
    """The waits of a drain, or None when ``REMEMBRA_RELAY_DRAIN`` turns it off. A value that is not a list of
    seconds counts as unset."""
    raw = ((os.environ if environ is None else environ).get(DRAIN_ENV) or "").strip().lower()
    if raw in ("off", "0", "no", "false"):
        return None
    try:
        waits = tuple(min(max(float(part), 0.0), DRAIN_MAX_SECONDS) for part in raw.split(",") if part.strip())
    except ValueError:
        waits = ()
    return waits or DRAIN_WAITS_SECONDS


def drain_lock_path(home: Path) -> Path:
    return relay_dir(home) / "drain.lock"


def drain_kick_path(home: Path) -> Path:
    return relay_dir(home) / "drain.kick"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - no cheap liveness check: the lock's age decides
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists and belongs to someone else
    return True


def drain_running(home: Path) -> bool:
    """Whether a live drain holds the lock: its process exists and it kept the lock fresh (:func:`touch_drain_lock`)."""
    path = drain_lock_path(home)
    try:
        age = time.time() - path.stat().st_mtime
        pid = int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return False
    return age < DRAIN_LOCK_STALE_SECONDS and _pid_alive(pid)


def touch_drain_lock(home: Path) -> None:
    """The running drain says it is still there (the lock's mtime). Only the lock's owner; never raises."""
    path = drain_lock_path(home)
    try:
        if int(path.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            os.utime(path)
    except (OSError, ValueError):
        pass


def acquire_drain_lock(home: Path) -> bool:
    """Take the single drain slot of this HOME (a pid file). Never raises.

    False when a live drain has it, and also when the lock cannot be written at all (a full or read-only
    disk): :func:`drain_running` tells the two apart, and only the first is a reason to :func:`tell_drain`.
    """
    path = drain_lock_path(home)
    try:
        _ensure_dir(path.parent)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if drain_running(home):
                    return False
                with contextlib.suppress(OSError):
                    path.unlink()  # left by a drain that was killed
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return True
    except OSError:
        pass
    return False


def release_drain_lock(home: Path) -> None:
    path = drain_lock_path(home)
    try:
        if int(path.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def read_drain_kick(home: Path) -> tuple[float, float] | None:
    """``(when it was asked, not before when)`` of the newest request to a running drain, or None."""
    path = drain_kick_path(home)
    try:
        return path.stat().st_mtime, float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def tell_drain(home: Path, not_before: float) -> None:
    """Ask the running drain to go again, not before ``not_before`` (it reads the request within a second)."""
    atomic_write_private(drain_kick_path(home), str(not_before))


def _worth_a_drain(path: Path, environ: Mapping[str, str], home: Path) -> bool:
    """A queued entry that trying again may deliver (:func:`sendable`, in the environment the drain would get)."""
    entry = read_entry(path)
    return entry is not None and sendable(entry, environ, home)


def kick_drain(home: Path, *, now: bool = False, environ: Mapping[str, str] | None = None) -> bool:
    """Have the queue sent in the background: start a detached ``remembra-relay drain``, or tell the running one.

    ``now`` is for a caller that has no reason to think the server is unreachable (a session start or end
    finding older entries): the first try is immediate. Without it the first try waits (the caller's own
    request has just failed). Nothing happens when the drain is turned off (``REMEMBRA_RELAY_DRAIN=off``) or
    nothing is queued that a drain could send (:func:`sendable`). True when a drain is running or was started.
    Never raises, never blocks: the child
    has its own session and no pipe of the caller's, so a hook returns at once.
    """
    env = os.environ if environ is None else environ
    try:
        waits = drain_waits(env)
        child_env = {**env, "HOME": str(home)}
        if waits is None or not any(_worth_a_drain(path, child_env, home) for path in outbox_dir(home).glob("*.json")):
            return False
        if drain_running(home):
            tell_drain(home, time.time() + (0.0 if now else waits[0]))
            return True
        argv = [sys.executable, "-m", "remembra.relay.cli", "drain", *(["--now"] if now else [])]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=child_env,
            **kwargs,
        )
        return True
    except Exception as e:
        log(home, f"drain: could not start ({e.__class__.__name__})")
        return False


# ---------------------------------------------------------------------------
# Status (last success / failure per agent, key state per config source)
# ---------------------------------------------------------------------------


def read_status(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(status_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


STATUS_LOCK_WAIT_SECONDS = 0.5  # a hook never waits longer for another hook's status write
STATUS_LOCK_STALE_SECONDS = 5.0


@contextlib.contextmanager
def _status_lock(home: Path) -> Iterator[None]:
    """Serialise the read-modify-write of status.json across hooks that end in the same second.

    Bounded: after ``STATUS_LOCK_WAIT_SECONDS`` (or when the lock cannot be made) the caller goes ahead
    without it, as before; a lock left by a killed process is removed after ``STATUS_LOCK_STALE_SECONDS``.
    Never raises.
    """
    path = status_path(home).with_name("status.lock")
    held = False
    try:
        _ensure_dir(path.parent)
        end = time.monotonic() + STATUS_LOCK_WAIT_SECONDS
        while True:
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                held = True
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - path.stat().st_mtime > STATUS_LOCK_STALE_SECONDS:
                        path.unlink()
                        continue
                if time.monotonic() >= end:
                    break
                time.sleep(0.01)
    except OSError:
        pass
    try:
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                path.unlink()


def _record_key(data: dict[str, Any], config_source: str | None, ok: bool, http_status: int | None) -> None:
    if not config_source or not (ok or http_status in (401, 403)):
        return
    keys = data.get("keys")
    if not isinstance(keys, dict):
        keys = data["keys"] = {}
    keys[config_source] = {
        "state": "accepted" if ok else ("rejected" if http_status == 401 else "refused"),
        "http_status": None if ok else http_status,
        "at": _now_iso(),
        "ts": time.time(),
    }


def record_key(home: Path, *, config_source: str | None, ok: bool, http_status: int | None = None) -> None:
    """Remember whether the server accepted the key of ``config_source``, and nothing else.

    For a command that only checks the key (``remembra-relay status``): it is not a brief or a close, so
    no agent's last result changes. Never raises.
    """
    try:
        with _status_lock(home):
            data = read_status(home)
            _record_key(data, config_source, ok, http_status)
            atomic_write_private(status_path(home), json.dumps(data, indent=2, default=str))
    except Exception:
        pass


def record(
    home: Path,
    *,
    agent_id: str | None,
    command: str,
    ok: bool,
    config_source: str | None = None,
    error: str | None = None,
    http_status: int | None = None,
) -> None:
    """Remember the outcome of one call for ``remembra-relay status``. Never raises.

    Per agent: ``last_success`` and ``last_failure`` (the newest of any command, as older readers expect),
    the same two per command under ``by_command`` (a later close failure does not erase the brief's), and
    the last ``RECENT_FAILURES`` failures under ``recent_failures``, oldest first.
    """
    try:
        with _status_lock(home):
            data = read_status(home)
            agents = data.get("agents")
            if not isinstance(agents, dict):
                agents = data["agents"] = {}
            slot = agents.get(agent_id or "unknown-agent")
            if not isinstance(slot, dict):
                slot = agents[agent_id or "unknown-agent"] = {}
            commands = slot.get("by_command")
            if not isinstance(commands, dict):
                commands = slot["by_command"] = {}
            per_command = commands.get(command)
            if not isinstance(per_command, dict):
                per_command = commands[command] = {}
            event: dict[str, Any] = {"at": _now_iso(), "ts": time.time(), "command": command}
            if ok:
                slot["last_success"] = per_command["last_success"] = event
            else:
                event["error"] = str(redact(error or ""))[:300]
                event["http_status"] = http_status
                slot["last_failure"] = per_command["last_failure"] = event
                recent = slot.get("recent_failures")
                slot["recent_failures"] = [*(recent if isinstance(recent, list) else []), event][-RECENT_FAILURES:]
            _record_key(data, config_source, ok, http_status)
            atomic_write_private(status_path(home), json.dumps(data, indent=2, default=str))
    except Exception:
        pass
