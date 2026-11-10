"""Crew seats that outlive the MCP server process (stdio only).

A stdio MCP server is one OS process, and its crew session token used to live in that process's
memory only. When the client replaced the process (a request timed out, the app restarted), the
new process joined the crew as a new callsign while the old seat's claims stayed reserved for a
session nobody could act as. :class:`SeatStore` keeps each seat's token in a private file so the
next process can re-join as the same crew session: the server then keeps the callsign, renews the
leases and gives back what was reserved for that session.

**Ownership is a file lock.** The process that uses a seat holds an exclusive advisory lock on the
seat's ``.lock`` file for as long as it lives; the operating system drops the lock when the process
ends, however it ends. Another process may take a seat only by acquiring that lock, so a seat is
never used by two processes and a live process's seat is never taken (several Codex threads, two
desktop children). No process ids are compared, so a reused pid cannot confuse it.

**Which seat a process may resume.** Only one stored for the same server, API key, agent and
project (the *pool*) and the same *anchor*:

* an anchor names one conversation (a fixed ``REMEMBRA_SESSION_ID``, or the client's conversation
  id): its seat is resumed for up to :data:`ANCHORED_RESUME_S`;
* without an anchor (a desktop app's server has none) the seat of the previous process of this
  agent on this machine is resumed, but only while the server has not yet declared it lost
  (:data:`POOL_RESUME_S`). After that the server has written the handoff and offers the work as a
  baton, and the new process joins fresh.

The files hold a crew session token (not the API key). The directory is ``0700`` and the files
``0600``; nothing here is ever shown to the agent. A remote MCP server never uses a store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

RECORD_VERSION: Final = 1
# The server marks a silent MCP session lost after 60 minutes and then hands its work on as a baton.
POOL_RESUME_S: Final = 55 * 60.0
ANCHORED_RESUME_S: Final = 24 * 3600.0
# Records nobody could resume any more are removed after this long.
STALE_AFTER_S: Final = ANCHORED_RESUME_S
MAX_FILES: Final = 2000


def pool_key(base_url: str, fingerprint: str, agent_id: str, project_id: str) -> str:
    """One pool per server, API key (by fingerprint), agent and project: a seat is never resumed across them."""
    raw = "\0".join((base_url.rstrip("/"), fingerprint, agent_id, project_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class SeatRecord:
    """What a later process needs to re-join as the same crew session."""

    pool: str
    anchor: str
    project_id: str
    agent_id: str
    client_session_id: str
    crew_id: str
    session_id: str
    callsign: str
    token: str
    verified: bool
    saved_at: float = 0.0
    version: int = RECORD_VERSION

    @property
    def stem(self) -> str:
        raw = f"{self.pool}\0{self.client_session_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _record_from(data: Any) -> SeatRecord | None:
    if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
        return None
    text = ("pool", "anchor", "project_id", "agent_id", "client_session_id", "crew_id", "session_id", "callsign", "token")
    if any(not isinstance(data.get(k), str) for k in text):
        return None
    if not all(data[k] for k in ("pool", "client_session_id", "crew_id", "session_id", "token")):
        return None
    saved_at = data.get("saved_at")
    if isinstance(saved_at, bool) or not isinstance(saved_at, (int, float)):
        return None
    return SeatRecord(
        **{k: data[k] for k in text},
        verified=bool(data.get("verified")),
        saved_at=float(saved_at),
    )


def _lock(fd: int) -> bool:
    """Take an exclusive lock on ``fd`` without waiting. False when another open file holds it."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - no locking primitive: never claim ownership
            return False
    except OSError:
        return False
    return True


class SeatStore:
    """Seat files of one directory, and the locks this process holds on them. Never raises on disk errors."""

    def __init__(self, root: Path, *, anchor: str = "", now: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.anchor = anchor
        self._now = now
        self._guard = threading.RLock()
        self._held: dict[str, int] = {}  # stem -> the lock file's descriptor, open for this process's life

    # -- files ----------------------------------------------------------------------

    def _json(self, stem: str) -> Path:
        return self.root / f"{stem}.json"

    def _lockfile(self, stem: str) -> Path:
        return self.root / f"{stem}.lock"

    def _ensure_root(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                os.chmod(self.root, 0o700)
        except OSError:
            return False
        return True

    def _acquire(self, stem: str, *, fresh: bool = False) -> bool:
        """Hold the seat's lock: True when this process holds it now, or already did unless ``fresh``."""
        with self._guard:
            if stem in self._held:
                return not fresh
            path = self._lockfile(stem)
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                return False
            ok = _lock(fd)
            if ok:
                # the file may have been removed and re-created between open and lock: the lock must be
                # on the file the path names now, or two processes could each hold "the" lock
                try:
                    ok = os.fstat(fd).st_ino == os.stat(path).st_ino
                except OSError:
                    ok = False
            if not ok:
                with contextlib.suppress(OSError):
                    os.close(fd)
                return False
            self._held[stem] = fd
            return True

    def _release(self, stem: str, *, remove: bool = False) -> None:
        with self._guard:
            fd = self._held.pop(stem, None)
            if fd is None:
                return
            if remove:  # while still locked, so no other process can be holding it
                with contextlib.suppress(OSError):
                    os.unlink(self._lockfile(stem))
            with contextlib.suppress(OSError):
                os.close(fd)

    def _read(self, path: Path) -> SeatRecord | None:
        try:
            return _record_from(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def _write(self, record: SeatRecord) -> bool:
        path = self._json(record.stem)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(record), fh)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return False
        return True

    # -- the seat's life --------------------------------------------------------------

    def save(self, record: SeatRecord) -> bool:
        """Write (or refresh) this process's seat. False when the seat cannot be owned or written."""
        with self._guard:
            if not self._ensure_root() or not self._acquire(record.stem):
                return False
            return self._write(replace(record, anchor=self.anchor, saved_at=self._now()))

    def drop(self, record: SeatRecord) -> None:
        """Forget a seat this process owns: it left the crew, or the server no longer accepts its token."""
        with self._guard:
            if record.stem not in self._held and not self._acquire(record.stem):
                return
            with contextlib.suppress(OSError):
                os.unlink(self._json(record.stem))
            self._release(record.stem, remove=True)

    def release(self, record: SeatRecord) -> None:
        """Give a seat back untouched (the server could not be reached): a later call may take it again."""
        self._release(record.stem)

    def take(self, pool: str) -> Iterator[SeatRecord]:
        """Seats of ``pool`` whose owner is gone, newest first; each one yielded is now owned by this process.

        The caller re-joins with the record and then calls :meth:`save` (it worked), :meth:`drop` (the
        server refused it) or :meth:`release` (try again later).
        """
        limit = ANCHORED_RESUME_S if self.anchor else POOL_RESUME_S
        now = self._now()
        candidates: list[SeatRecord] = []
        try:
            paths = sorted(self.root.glob("*.json"))[:MAX_FILES]
        except OSError:
            return
        for path in paths:
            record = self._read(path)
            stem = path.stem
            with self._guard:
                mine = stem in self._held
            if record is None or record.stem != stem:
                self._discard(stem, age=self._age(path, now), record_ok=False)
                continue
            age = now - record.saved_at
            if age > STALE_AFTER_S:
                self._discard(stem, age=age, record_ok=True)
                continue
            if mine or record.pool != pool or record.anchor != self.anchor:
                continue
            if age > limit:
                self._discard(stem, age=STALE_AFTER_S + 1, record_ok=True)  # resumable by nobody any more
                continue
            candidates.append(record)
        for record in sorted(candidates, key=lambda r: r.saved_at, reverse=True):
            if not self._acquire(record.stem, fresh=True):
                continue  # its owner is alive (or another call of this process is resuming it right now)
            fresh = self._read(self._json(record.stem))  # the owner may have written it once more before it died
            if fresh is None or fresh.pool != pool or fresh.anchor != self.anchor:
                self._release(record.stem)
                continue
            yield fresh

    def _age(self, path: Path, now: float) -> float:
        try:
            return now - path.stat().st_mtime
        except OSError:
            return 0.0

    def _discard(self, stem: str, *, age: float, record_ok: bool) -> None:
        """Remove a record nobody can use (unreadable, or too old) once its owner is gone."""
        if record_ok and age <= STALE_AFTER_S:
            return
        if not record_ok and age < 60.0:
            return  # possibly being written right now
        with self._guard:
            if stem in self._held or not self._acquire(stem):
                return
            with contextlib.suppress(OSError):
                os.unlink(self._json(stem))
            self._release(stem, remove=True)

    def close(self) -> None:
        """Release every lock, as the operating system does when the process ends. The files stay."""
        with self._guard:
            for stem in list(self._held):
                self._release(stem)
