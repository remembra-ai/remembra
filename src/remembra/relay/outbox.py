"""Local outbox, log and status for ``remembra-relay``: a handoff is never lost silently.

When ``close`` cannot deliver a handoff (server down, timeout, 5xx, 429, a
rejected key, no key yet) the request body is queued in
``~/.remembra/relay/outbox/<agent>-<session>.json`` and a line goes to
``~/.remembra/relay/relay.log``. Every later ``brief`` and ``close`` sends the
queue again. The server keeps one current handoff per (agent, session): a
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
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from remembra.relay.handoff import redact

MAX_ENTRIES = 50
MAX_ENTRY_BYTES = 256 * 1024
MAX_AGE_SECONDS = 14 * 86400
LOG_MAX_BYTES = 256 * 1024
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


def log(home: Path, message: str) -> None:
    """Append one line to relay.log (0600, rotated to relay.log.1 past ``LOG_MAX_BYTES``). Never raises."""
    try:
        path = log_path(home)
        _ensure_dir(path.parent)
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
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
        return max(count, 1) if self.data.get("last_status") == 403 else max(count, 0)

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


def refusal_hold(entry: Entry, now: float | None = None) -> tuple[str, str] | None:
    """Why a refused (403) entry is not sent right now: ``(kind, reason)``, or None when it may go.

    ``("agent", ...)``: the key is scoped to another agent than the one the handoff is from. That key can
    never send it, so it is never sent on its own again (``remembra-relay outbox send`` or ``drop`` decide).
    ``("backoff", ...)``: any other refusal (the key may not write to that project) waits
    ``REFUSAL_BACKOFF_SECONDS`` after each refusal instead of going out with every brief and close.
    An entry with no time recorded at all is not held by the backoff.
    """
    if entry.data.get("last_status") != 403:
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
) -> Path | None:
    """Queue a close body that could not be delivered. Returns the entry path, or None when it was not kept.

    ``api_key`` is the key the close was tried with. It is never stored: when the server refused the close
    (403), the entry keeps that key's :func:`key_mark` as ``refused_key_fp`` until it is delivered or dropped.
    ``extra`` adds fields of the queuing program to the entry (the MCP server's ``key_fp``, see
    :mod:`remembra.mcp.close_queue`); it never replaces a field written here.
    """
    agent_id = str(payload.get("agent_id") or "unknown-agent")
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        log(home, f"outbox: not queued, no session id (agent {agent_id})")
        return None
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
            "refused_key_fp": key_mark(api_key)
            if http_status == 403
            else (previous.data.get("refused_key_fp") if previous else None),
            "last_error": str(redact(error))[:300],
            "last_status": http_status,
            "refusals": (previous.refusals if previous else 0) + (1 if http_status == 403 else 0),
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
) -> None:
    """After a send: delete the claim when delivered, else put it back (a newer queued close wins).

    A 403 records the :func:`key_mark` of ``api_key``, the key that was refused; a later failure of
    another kind does not clear it.
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
        if http_status == 403:
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


def clear_queued_failure(home: Path, agent_id: str) -> bool:
    """Forget ``agent_id``'s recorded close failure once nothing of it is queued (status and doctor list it otherwise).

    True when a record was removed. Never raises.
    """
    try:
        if any(e.agent_id == agent_id for e in pending(home)):
            return False
        data = read_status(home)
        slot = (data.get("agents") or {}).get(agent_id)
        if not isinstance(slot, dict):
            return False
        failure = slot.get("last_failure")
        if not isinstance(failure, dict) or not str(failure.get("command") or "").startswith("close"):
            return False
        del slot["last_failure"]
        atomic_write_private(status_path(home), json.dumps(data, indent=2, default=str))
        return True
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


# ---------------------------------------------------------------------------
# Status (last success / failure per agent, key state per config source)
# ---------------------------------------------------------------------------


def read_status(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(status_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
    """Remember the outcome of one call for ``remembra-relay status``. Never raises."""
    try:
        data = read_status(home)
        agents = data.setdefault("agents", {})
        slot = agents.setdefault(agent_id or "unknown-agent", {})
        event: dict[str, Any] = {"at": _now_iso(), "ts": time.time(), "command": command}
        if ok:
            slot["last_success"] = event
        else:
            event["error"] = str(redact(error or ""))[:300]
            event["http_status"] = http_status
            slot["last_failure"] = event
        if config_source and (ok or http_status in (401, 403)):
            keys = data.setdefault("keys", {})
            keys[config_source] = {
                "state": "accepted" if ok else ("rejected" if http_status == 401 else "refused"),
                "http_status": None if ok else http_status,
                "at": _now_iso(),
                "ts": time.time(),
            }
        atomic_write_private(status_path(home), json.dumps(data, indent=2, default=str))
    except Exception:
        pass
