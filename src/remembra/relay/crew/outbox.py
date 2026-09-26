"""The crew outbox: spooled records that must reach the server (spec §8.1 ``outbox/*.json``).

Anything the local runtime could not deliver at once is written here as one JSON file and
replayed by crewd every 5 s (``flush``): client events (``guard.blocked``, ``gate.deadline``, …),
``unconfirmed`` auto-claims (D11), checkpoints, activity from a gate that found crewd down,
stall and leave calls. Every record carries an idempotency key chosen when it was spooled, so a
record replayed twice never produces two server rows (§13.2 "Outbox replayed twice").

Stdlib only: the vendored gate spools here when crewd is unreachable.

File layout: ``<created_ns>-<kind>-<id>.json`` (sorted = spool order), or a fixed name for
records that replace each other (the pending unconfirmed claim of one session and zone:
``claim-<session_key>-<zone>.json``). Writes are atomic (temp file + rename), files are 0600
and the directory 0700.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

OUTBOX_VERSION: Final = 1
KINDS: Final = ("event", "claim", "checkpoint", "activity", "posttool", "stall", "leave", "close")
MAX_AGE_S: Final = 72 * 3600  # an undeliverable record is dropped after the idempotency window (§4.3)
MAX_ATTEMPTS: Final = 500
BACKOFF_BASE_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0
MAX_RECORD_BYTES: Final = 64 * 1024
_SAFE_RE: Final = re.compile(r"[^A-Za-z0-9_.-]+")

SendResult = str  # "done" | "retry" | "drop"
Sender = Callable[["Entry"], Awaitable[SendResult]]


def _safe(text: str, limit: int = 64) -> str:
    return _SAFE_RE.sub("_", text)[:limit] or "x"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, data: Any, *, mode: int = 0o600) -> None:
    """Write JSON atomically (same-directory temp file, fsync-free rename) with ``mode``."""
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), sort_keys=True, default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Entry:
    """One spooled record."""

    path: Path
    kind: str
    session_key: str | None
    crew_id: str | None
    idem: str
    created_at: float
    body: dict[str, Any]
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str | None = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.created_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": OUTBOX_VERSION,
            "kind": self.kind,
            "session_key": self.session_key,
            "crew_id": self.crew_id,
            "idem": self.idem,
            "created_at": self.created_at,
            "body": self.body,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
        }


def spool(
    outbox_dir: Path,
    kind: str,
    body: dict[str, Any],
    *,
    session_key: str | None = None,
    crew_id: str | None = None,
    name: str | None = None,
    idem: str | None = None,
    now: float | None = None,
) -> Path:
    """Spool one record; returns its path. ``name`` makes the record replace an earlier one of that name."""
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind!r}")
    created = time.time() if now is None else now
    ident = idem or f"ob{secrets.token_hex(10)}"
    record = Entry(
        path=Path(),
        kind=kind,
        session_key=session_key,
        crew_id=crew_id,
        idem=ident,
        created_at=created,
        body=body,
    ).to_json()
    raw = json.dumps(record, separators=(",", ":"), default=str)
    if len(raw.encode()) > MAX_RECORD_BYTES:
        raise ValueError(f"outbox record too large ({len(raw)} bytes)")
    filename = f"{_safe(name, 120)}.json" if name else f"{int(created * 1e9):020d}-{kind}-{ident}.json"
    path = ensure_dir(outbox_dir) / filename
    atomic_write_json(path, record)
    return path


def claim_record_name(session_key: str, zone_id: str | None, path_glob: str | None = None) -> str:
    target = zone_id or ("file-" + _safe(path_glob or "", 80))
    return f"claim-{_safe(session_key)}-{_safe(target, 100)}"


def _load(path: Path) -> Entry | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in KINDS or not isinstance(data.get("body"), dict):
        return None
    return Entry(
        path=path,
        kind=str(data["kind"]),
        session_key=data.get("session_key"),
        crew_id=data.get("crew_id"),
        idem=str(data.get("idem") or path.stem),
        created_at=float(data.get("created_at") or 0.0),
        body=data["body"],
        attempts=int(data.get("attempts") or 0),
        next_attempt_at=float(data.get("next_attempt_at") or 0.0),
        last_error=data.get("last_error"),
    )


def read_entries(outbox_dir: Path, kinds: Iterable[str] | None = None) -> list[Entry]:
    """Every readable record, oldest first. Unreadable files (a crash mid-write left a temp file) are skipped."""
    wanted = set(kinds) if kinds is not None else None
    try:
        names = [p for p in outbox_dir.iterdir() if p.suffix == ".json" and not p.name.startswith(".")]
    except OSError:
        return []
    out: list[Entry] = []
    for path in names:
        entry = _load(path)
        if entry is None or (wanted is not None and entry.kind not in wanted):
            continue
        out.append(entry)
    out.sort(key=lambda e: (e.created_at, e.path.name))
    return out


def remove(entry: Entry) -> None:
    try:
        entry.path.unlink()
    except OSError:
        pass


def pending_claims(outbox_dir: Path, session_key: str) -> list[Entry]:
    """Unconfirmed claims of one session still waiting in the outbox (D11: the zone stays closed until confirmed)."""
    prefix = f"claim-{_safe(session_key)}-"
    return [e for e in read_entries(outbox_dir, ("claim",)) if e.path.name.startswith(prefix)]


def pending_claim_zone_ids(outbox_dir: Path, session_key: str) -> set[str]:
    return {str(e.body.get("zone_id")) for e in pending_claims(outbox_dir, session_key) if e.body.get("zone_id")}


@dataclass
class FlushResult:
    sent: int = 0
    retried: int = 0
    dropped: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _backoff(attempts: int) -> float:
    return float(min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** min(attempts, 8))))


async def flush(
    outbox_dir: Path,
    send: Sender,
    *,
    now: float | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = 200,
    stop_on_retry: bool = True,
) -> FlushResult:
    """Replay spooled records in order through ``send``.

    ``send`` returns ``"done"`` (delivered or already delivered: the record is removed), ``"drop"``
    (permanently refused, e.g. 422: removed and counted) or ``"retry"`` (unreachable, 5xx or 429:
    kept with exponential backoff). With ``stop_on_retry`` a retry holds back the later records of
    the same kind, so each kind is replayed in spool order while the server is down, and one kind
    that cannot be delivered never blocks the others.
    """
    t = time.time() if now is None else now
    result = FlushResult()
    held: set[str] = set()
    for entry in read_entries(outbox_dir, kinds)[:limit]:
        if t - entry.created_at > MAX_AGE_S or entry.attempts >= MAX_ATTEMPTS:
            remove(entry)
            result.dropped += 1
            continue
        if entry.kind in held or entry.next_attempt_at > t:
            result.skipped += 1
            if stop_on_retry:
                held.add(entry.kind)
            continue
        try:
            outcome = await send(entry)
        except Exception as e:  # a sender bug must not wedge the outbox
            outcome = "retry"
            entry.last_error = f"{e.__class__.__name__}"
            result.errors.append(entry.last_error)
        if outcome == "done":
            remove(entry)
            result.sent += 1
        elif outcome == "drop":
            remove(entry)
            result.dropped += 1
        else:
            entry.attempts += 1
            entry.next_attempt_at = t + _backoff(entry.attempts)
            try:
                atomic_write_json(entry.path, entry.to_json())
            except OSError:
                pass
            result.retried += 1
            if stop_on_retry:
                held.add(entry.kind)
    return result
