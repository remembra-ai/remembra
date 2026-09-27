"""Split a project that several repositories share (``remembra-relay projects split``).

Before 0.16.1 a configured project (``REMEMBRA_PROJECT``) named every git
repository the server had not seen yet, so for users who had one, every
repository's handoffs went into one trail and a brief in one repository could
hand over another repository's work. A split gives each repository bound to
that project its own project again and re-files the handoffs recorded there:

* **Repositories.** Every ``git`` binding of the project is a repository. The
  ``root`` and ``path`` bindings written by the same resolve (same
  ``created_at``) belong to it, and so do the ones a checkout the client read
  with git shows (the client sends those as ``checkouts``). A ``root`` binding
  with no remote is a repository of its own. ``path`` bindings of folders stay:
  the configured project still names folders that are not repositories.
* **Target project.** The id a new repository gets: its name, or
  ``<name>-<hash>`` when that is taken (the rule of
  :meth:`~remembra.services.relay.ProjectRegistry._derive_new_id`). A
  repository whose name is the project itself keeps it.
* **Handoffs move only on evidence.** (1) The location recorded with the
  handoff (the server records it at close since 0.16.1) is one of the
  repositories; or (2) for a handoff closed before that, the client read with
  git that a commit the handoff recorded (its session commits, else its HEAD)
  exists in a checkout of exactly one of the repositories, and every other
  repository that may share that history (the same root commit, or one not
  on record) was read too: a fork or mirror that is not checked out here
  could hold the same commit. Earlier versions of a moved handoff (same agent
  and session) go with it. Everything else stays where it is and is listed
  with the reason; checkpoints record no location and stay.
* **Safe.** Dry run by default. Applying plans and moves in one transaction,
  records every change in ``relay_refiles`` (batch, kind, what, from, to,
  evidence) and one audit event, and :func:`undo` reverses a batch. Applying
  again moves only what is still there and matched, so it is idempotent.
  Nothing is deleted. Every query is scoped to the caller's user id. A split
  never runs while a close of the same account is in flight
  (:func:`user_gate`), so a session's handoff is never left behind in the old
  project. API keys and connections restricted to the project would lose the
  split repositories: they are listed, and applying needs the caller's
  confirmation (``restricted_keys_lose_access``).
* **Output.** Everything the split returns that a client or agent wrote
  (paths, names, headlines, branches) passes the brief's trust policy: a
  low-trust handoff keeps only its ids and times, a low-trust path or name is
  replaced by a withheld note.

The ``relay_refiles`` table is created on first use (like the security state
tables), so no schema migration is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.relay.handoff import (
    assess_text,
    handoff_headline,
    handoff_location,
    handoff_verdict,
    relay_block,
    show_text,
)
from remembra.relay.identity import (
    KIND_GIT,
    KIND_PATH,
    KIND_ROOT,
    ProjectLocator,
    repository_key,
    slugify_project,
)
from remembra.services.agent_session import HANDOFF_ENDED_JD, _parse_metadata

log = structlog.get_logger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_LISTED_STAYS = 500
MAX_COMMITS_PER_HANDOFF = 30  # the (at most 30) commits a handoff's relay block keeps, else its HEAD
WITHHELD_TEXT = "(withheld: low trust)"  # in place of client-written text the trust policy withholds
# relay_refiles kinds: moves undo reverses, and records that only describe the batch.
MOVE_KINDS = ("binding", "memory")
KIND_PROJECT = "project"  # a target project this batch created (undo moves everything of its repository back)
KIND_SUPERSEDE = "supersede"  # an older current copy of a moved session, superseded (one current handoff per session)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_refiles (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    from_project TEXT NOT NULL,
    to_project TEXT NOT NULL,
    evidence TEXT,
    created_at TEXT NOT NULL,
    undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_relay_refiles_user_batch ON relay_refiles(user_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_relay_refiles_user_from ON relay_refiles(user_id, from_project);
"""

_initialized: weakref.WeakSet[Any] = weakref.WeakSet()


async def ensure_schema(db: Any) -> None:
    """Create ``relay_refiles`` (the split log) if it does not exist yet."""
    conn = db.conn
    if conn in _initialized:
        return
    await conn.executescript(_SCHEMA)
    await conn.commit()
    _initialized.add(conn)


class UserGate:
    """Keeps a split or undo of an account from running while one of its closes is in flight (this process).

    A close resolves its project and then stores its handoff (superseding the
    session's earlier one in that project). A split between the two would move
    the earlier version and leave the new one in the old project, or two
    current handoffs for one session. Closes hold the gate shared (any number
    at once); a split holds it exclusively: it waits for the closes in flight,
    and closes that arrive meanwhile wait for it, then resolve with the new
    bindings.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and not self._waiting)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._cond:
            self._waiting += 1
            try:
                await self._cond.wait_for(lambda: not self._writer and not self._readers)
            finally:
                self._waiting -= 1
                self._cond.notify_all()  # a cancelled split must not keep closes waiting
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


_gates: weakref.WeakValueDictionary[str, UserGate] = weakref.WeakValueDictionary()


def user_gate(user_id: str) -> UserGate:
    """The account's :class:`UserGate` (one per user id while anyone holds it)."""
    gate = _gates.get(user_id)
    if gate is None:
        gate = UserGate()
        _gates[user_id] = gate
    return gate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe(text: Any, limit: int = 400) -> Any:
    """Client-written text as the split returns it: the brief's trust policy (low trust gives
    :data:`WITHHELD_TEXT`), hidden characters removed. Identifiers are not flagged as commands."""
    if not isinstance(text, str) or not text:
        return text
    if assess_text(text).withheld:
        return WITHHELD_TEXT
    return show_text(text, None, limit)


def _safe_key(key: str) -> str:
    """A fingerprint key for display: ``kind:`` and its value under :func:`_safe`."""
    kind, sep, value = key.partition(":")
    return f"{kind}{sep}{_safe(value)}" if sep else str(_safe(key))


def _safe_place(place: dict[str, Any]) -> dict[str, Any]:
    """A ``{host, path}`` for the client to read with git: withheld (no path) when the policy withholds it.
    The path is otherwise returned as recorded, so the client can find the directory."""
    if assess_text(place.get("host"), place.get("path")).withheld:
        return {"host": None, "path": None, "withheld": True}
    return place


def _value(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def _path_parts(key: str) -> dict[str, str | None]:
    """``{"host", "path"}`` of a ``path:`` fingerprint key (``path:host:/abs`` or ``path:/abs``)."""
    body = _value(key)
    if body.startswith("/") or re.match(r"^[A-Za-z]:/", body):
        return {"host": None, "path": body}
    host, _, path = body.partition(":")
    return {"host": host or None, "path": path or None}


def _repository_name(key: str, fingerprints: list[str]) -> str | None:
    if key.startswith(f"{KIND_GIT}:"):
        return _value(key).rstrip("/").rsplit("/", 1)[-1] or None
    paths = [p for p in fingerprints if p.startswith(f"{KIND_PATH}:")]
    tails = {str(_path_parts(p)["path"] or "").rstrip("/").rsplit("/", 1)[-1] for p in paths} - {""}
    return next(iter(tails)) if len(tails) == 1 else None


@dataclass
class Repository:
    """One repository bound to the project being split."""

    key: str  # git:<host/owner/repo>, else root:<sha>
    fingerprints: list[str] = field(default_factory=list)  # its bindings still in the project (they move)
    moved: list[str] = field(default_factory=list)  # its bindings an earlier split already moved to ``target``
    first_seen: str = ""
    target: str = ""
    keeps: bool = False  # named like the project itself: it keeps the project
    checkouts: list[dict[str, Any]] = field(default_factory=list)
    roots: set[str] = field(default_factory=set)  # root-commit keys seen with it (recorded locations, checkouts)

    @property
    def already(self) -> bool:
        """Split out by an earlier batch (not undone)."""
        return bool(self.moved)

    @property
    def read(self) -> bool:
        """A checkout of it was read with git on the client's machine."""
        return bool(self.checkouts)

    @property
    def name(self) -> str | None:
        return _repository_name(self.key, [*self.fingerprints, *self.moved])

    def all_keys(self) -> set[str]:
        return {self.key, *self.fingerprints, *self.moved}

    def known_roots(self) -> set[str]:
        """Its root commits on record (empty when none is: then it may share history with any repository)."""
        return {k for k in self.all_keys() if k.startswith(f"{KIND_ROOT}:")} | self.roots

    def as_dict(self) -> dict[str, Any]:
        paths = [_path_parts(p) for p in [*self.fingerprints, *self.moved] if p.startswith(f"{KIND_PATH}:")]
        return {
            "key": _safe_key(self.key),
            "name": _safe(self.name, 120),
            "target_project": self.target,
            "bindings": sorted(_safe_key(k) for k in self.fingerprints),
            "already_split": self.already,
            "keeps_project": self.keeps,
            "paths": [_safe_place(p) for p in paths],
            "checkouts": self.checkouts,
        }


@dataclass
class Checkout:
    """A checkout the client read with git on its machine."""

    locator: ProjectLocator
    present: set[str]

    @property
    def keys(self) -> list[str]:
        return [fp.key for fp in self.locator.fingerprints()]

    def describe(self) -> dict[str, Any]:
        repo = repository_key(self.keys)
        return {
            "root_path": _safe(self.locator.root_path),
            "host": _safe(self.locator.host, 255),
            "repository": _safe_key(repo) if repo else None,
        }

    @property
    def roots(self) -> set[str]:
        return {k for k in self.keys if k.startswith(f"{KIND_ROOT}:")}


class SplitRefused(Exception):
    """The request cannot be carried out (bad batch id, nothing to undo, ...)."""


async def _bindings(db: Any, user_id: str, project: str) -> list[tuple[str, str, str]]:
    cursor = await db.conn.execute(
        "SELECT fingerprint, kind, created_at FROM project_fingerprints WHERE user_id = ? AND project_id = ? "
        "ORDER BY created_at, fingerprint",
        (user_id, project),
    )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in await cursor.fetchall()]


async def _bound_to(db: Any, user_id: str, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    cursor = await db.conn.execute(
        f"SELECT fingerprint, project_id FROM project_fingerprints WHERE user_id = ? AND fingerprint IN ({marks})",  # noqa: S608
        [user_id, *keys],
    )
    return {str(r[0]): str(r[1]) for r in await cursor.fetchall()}


async def _project_known(db: Any, user_id: str, project_id: str) -> bool:
    cursor = await db.conn.execute(
        "SELECT 1 FROM project_fingerprints WHERE user_id = ? AND project_id = ? LIMIT 1", (user_id, project_id)
    )
    return await cursor.fetchone() is not None


async def _earlier_splits(db: Any, user_id: str, project: str) -> dict[str, list[str]]:
    """``{to_project: [fingerprint, ...]}`` moved out of ``project`` by batches not undone, still bound there."""
    cursor = await db.conn.execute(
        "SELECT ref, to_project FROM relay_refiles WHERE user_id = ? AND from_project = ? AND kind = 'binding' "
        "AND undone_at IS NULL",
        (user_id, project),
    )
    moved = [(str(r[0]), str(r[1])) for r in await cursor.fetchall()]
    now_at = await _bound_to(db, user_id, sorted({ref for ref, _ in moved}))
    out: dict[str, list[str]] = {}
    for ref, target in moved:
        if now_at.get(ref) == target and ref not in out.get(target, []):
            out.setdefault(target, []).append(ref)
    return out


def _group(bindings: list[tuple[str, str, str]]) -> tuple[list[Repository], list[str]]:
    """Repositories from a project's bindings, and the path bindings of folders (which stay)."""
    by_time: dict[str, list[tuple[str, str]]] = {}
    for key, kind, created in bindings:
        by_time.setdefault(created, []).append((key, kind))
    repos: dict[str, Repository] = {}
    attached: set[str] = set()
    for key, kind, created in bindings:
        if kind == KIND_GIT:
            repos[key] = Repository(key=key, fingerprints=[key], first_seen=created)
            attached.add(key)
    # Bindings a single resolve wrote together (one created_at) belong to that call's repository.
    for group in by_time.values():
        gits = [k for k, kind in group if kind == KIND_GIT]
        if len(gits) == 1:
            for key, kind in group:
                if kind != KIND_GIT and key not in attached:
                    repos[gits[0]].fingerprints.append(key)
                    attached.add(key)
    for key, kind, created in bindings:
        if kind == KIND_ROOT and key not in attached:
            repo = Repository(key=key, fingerprints=[key], first_seen=created)
            attached.add(key)
            siblings = [k for k, kd in by_time.get(created, []) if kd == KIND_PATH and k not in attached]
            if len(siblings) == 1:  # a repository with no remote, resolved from its checkout
                repo.fingerprints.append(siblings[0])
                attached.add(siblings[0])
            repos[key] = repo
    folders = [key for key, kind, _ in bindings if key not in attached]
    return list(repos.values()), folders


def _apply_checkouts(repos: list[Repository], folders: list[str], checkouts: list[Checkout]) -> dict[int, Repository]:
    """Tie each checkout to its repository; its root and path bindings join that repository.

    A root-commit repository whose checkout shows a remote bound here is the
    same repository (the remote was added later): the two are merged. Returns
    ``{index of checkout: repository}``.
    """
    matched: dict[int, Repository] = {}
    for index, checkout in enumerate(checkouts):
        keys = checkout.keys
        git = [k for k in keys if k.startswith(f"{KIND_GIT}:")]
        root = [k for k in keys if k.startswith(f"{KIND_ROOT}:")]
        repo = next((r for r in repos if git and git[0] in r.all_keys()), None)
        if repo is None and root:
            repo = next((r for r in repos if root[0] in r.all_keys()), None)
        if repo is None:
            continue
        matched[index] = repo
        for other in [r for r in repos if r is not repo and not r.moved and root and root[0] in r.all_keys()]:
            if other.key.startswith(f"{KIND_ROOT}:"):
                repo.fingerprints.extend(k for k in other.fingerprints if k not in repo.fingerprints)
                repos.remove(other)
        for key in keys:
            if key.startswith(f"{KIND_PATH}:") and key in folders:
                folders.remove(key)
                repo.fingerprints.append(key)
        repo.roots |= checkout.roots
        repo.checkouts.append(checkout.describe())
    return matched


def _link_by_recorded_locations(repos: list[Repository], folders: list[str], records: list[dict[str, Any]]) -> None:
    """Join bindings that one recorded location shows belong together.

    A handoff's recorded location lists every fingerprint of the checkout it
    was closed in (remote, root commit, path). When it names a repository's
    remote, a root-commit repository with the same root (the remote was added
    later) and a folder binding with the same path are that repository's too.
    """
    for memory in records:
        location = handoff_location(memory)
        keys = [str(k) for k in (location or {}).get("fingerprints") or []]
        git = next((k for k in keys if k.startswith(f"{KIND_GIT}:")), None)
        owner = next((r for r in repos if git and git in r.all_keys()), None)
        if owner is None:
            continue
        for key in keys:
            if key.startswith(f"{KIND_ROOT}:"):
                owner.roots.add(key)
                for other in [r for r in repos if r is not owner and not r.moved and r.key == key]:
                    owner.fingerprints.extend(k for k in other.fingerprints if k not in owner.fingerprints)
                    repos.remove(other)
            elif key.startswith(f"{KIND_PATH}:") and key in folders:
                folders.remove(key)
                owner.fingerprints.append(key)


async def _assign_targets(db: Any, user_id: str, project: str, repos: list[Repository]) -> None:
    reserved: set[str] = set()
    for repo in sorted(repos, key=lambda r: (r.already, r.first_seen, r.key)):
        if repo.already:
            reserved.add(repo.target)
            continue
        name = repo.name
        base = slugify_project(name if name else f"repo-{_value(repo.key)[:7]}")
        if base == project and not any(r.keeps for r in repos):
            repo.target, repo.keeps = project, True
            continue
        if base not in reserved and base != project and not await _project_known(db, user_id, base):
            repo.target = base
        else:
            repo.target = f"{base[:57]}-{hashlib.sha256(repo.key.encode()).hexdigest()[:6]}"
        reserved.add(repo.target)


def _valid_shas(values: list[Any]) -> list[str]:
    out: list[str] = []
    for sha in values:
        value = str(sha or "").strip().lower()
        if _SHA_RE.match(value) and value not in out:
            out.append(value)
    return out[:MAX_COMMITS_PER_HANDOFF]


def _commits_of(relay: dict[str, Any]) -> list[str]:
    """The commits that tie an older handoff to a repository: the session's own commits when it
    recorded any, else its HEAD (a HEAD is often a commit the repository shares with forks)."""
    own = _valid_shas([c.get("sha") for c in relay.get("commits") or [] if isinstance(c, dict)])
    return own or _valid_shas([relay.get("head_commit")])


async def _records(db: Any, user_id: str, project: str) -> list[dict[str, Any]]:
    cursor = await db.conn.execute(
        """
        SELECT id, project_id, memory_type, content, metadata, created_at, superseded_by, source, trust_score
        FROM memories
        WHERE user_id = ? AND project_id = ? AND memory_type IN ('handoff', 'checkpoint')
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY julianday(created_at) DESC, id DESC
        """,
        (user_id, project, datetime.now(UTC).replace(tzinfo=None).isoformat()),
    )
    rows = []
    for r in await cursor.fetchall():
        row = dict(r)
        row["metadata"] = _parse_metadata(row.get("metadata"))
        rows.append(row)
    return rows


def _summary(memory: dict[str, Any]) -> dict[str, Any]:
    """One handoff or checkpoint as the split lists it, under the brief's trust policy
    (:func:`~remembra.relay.handoff.handoff_verdict`: its text, upstream and recorded location). A
    withheld one keeps only its ids, times and commit; everything else passes :func:`_safe`."""
    meta = memory.get("metadata") or {}
    relay = relay_block(memory) or {}
    verdict = handoff_verdict(memory)
    item: dict[str, Any] = {
        "memory_id": memory["id"],
        "memory_type": memory.get("memory_type"),
        "agent_id": _safe(relay.get("agent_id") or meta.get("agent_id"), 60),
        "session_id": _safe(meta.get("session_id"), 80),
        "created_at": memory.get("created_at"),
        "head_commit": relay.get("head_commit") if _SHA_RE.match(str(relay.get("head_commit") or "")) else None,
        "withheld": verdict.withheld,
    }
    if verdict.withheld:
        return {**item, "branch": None, "headline": "withheld (low trust)", "location": None}
    location = handoff_location(memory)
    shown = None
    if location:
        shown = {k: _safe(location.get(k)) for k in ("name", "root_path", "host")}
        shown["repository"] = _safe_key(str(location["repository"])) if location.get("repository") else None
    return {
        **item,
        "branch": _safe(relay.get("branch"), 120),
        "headline": show_text(handoff_headline(memory), verdict, 120),
        "location": shown,
    }


def _match(
    memory: dict[str, Any],
    repos: list[Repository],
    checkouts: list[Checkout],
    matched_checkouts: dict[int, Repository],
    project: str,
) -> tuple[Repository | None, str, str]:
    """``(repository, how, evidence)`` a handoff goes to, or ``(None, "", why it stays)``."""
    if memory.get("memory_type") != "handoff":
        return None, "", "a checkpoint: no location is recorded with checkpoints"
    relay = relay_block(memory)
    if relay is None:
        return None, "", "a free-form handoff (stored through the memory API): no location or commit recorded"
    location = handoff_location(memory)
    if location is not None:
        keys = [str(k) for k in location.get("fingerprints") or []]
        repo_key = repository_key(keys)
        if repo_key is None:
            return None, "", f"worked in a folder (no git remote or root commit recorded); folders stay in '{project}'"
        found = [r for r in repos if r.all_keys() & {k for k in keys if not k.startswith(f"{KIND_PATH}:")}]
        if len(found) == 1:
            return found[0], "location", f"recorded location {_safe_key(repo_key)}"
        if not found:
            return None, "", f"its recorded repository ({_safe_key(repo_key)}) is not bound to '{project}'"
        return None, "", f"its recorded location matches {len(found)} repositories"
    shas = _commits_of(relay)
    if not shas:
        return None, "", "no location or commit recorded (closed before Remembra 0.16.1, outside git)"
    hits: dict[str, tuple[Repository, str, Checkout]] = {}
    for index, checkout in enumerate(checkouts):
        repo = matched_checkouts.get(index)
        if repo is None:
            continue
        sha = next((s for s in shas if s in checkout.present), None)
        if sha is not None and repo.key not in hits:
            hits[repo.key] = (repo, sha, checkout)
    if len(hits) == 1:
        repo, sha, checkout = next(iter(hits.values()))
        unread = _may_share_history(repo, checkout.roots | repo.known_roots(), repos)
        if unread:
            names = ", ".join(sorted(str(_safe(r.name or r.key, 80)) for r in unread))
            return (
                None,
                "",
                (
                    f"its commit {sha[:7]} is in the checkout of {_safe(repo.name or repo.key, 80)}, but {names} (not read "
                    "on this machine) may share that history (a fork or clone); pass --repo with a checkout of each"
                ),
            )
        where = checkout.describe()
        on = f" on {where['host']}" if where.get("host") else ""
        return repo, "commit", f"commit {sha[:7]} it recorded is in the checkout at {where['root_path']}{on} (read with git)"
    if len(hits) > 1:
        names = ", ".join(sorted(str(_safe(r.name or r.key, 80)) for r, _, _ in hits.values()))
        return None, "", f"its commits are in more than one repository ({names})"
    if checkouts:
        return None, "", "no location recorded (closed before Remembra 0.16.1), and none of its commits is in a checkout read"
    return None, "", "no location recorded (closed before Remembra 0.16.1); run the split where the repository is checked out"


def _may_share_history(hit: Repository, roots: set[str], repos: list[Repository]) -> list[Repository]:
    """The other repositories that were not read on the client's machine and may hold the same commits:
    the same root commit, or no root commit on record (one bound by its remote only: its root commit is
    already bound to another location, which a fork or clone of it would be)."""
    out = []
    for other in repos:
        if other is hit or other.read:
            continue
        theirs = other.known_roots()
        if not theirs or theirs & roots:
            out.append(other)
    return out


async def plan(db: Any, user_id: str, project: str, checkouts: list[Checkout] | None = None) -> dict[str, Any]:
    """What a split of ``project`` would do for ``user_id`` (read only)."""
    await ensure_schema(db)
    checkouts = checkouts or []
    repos, folders = _group(await _bindings(db, user_id, project))
    for target, keys in (await _earlier_splits(db, user_id, project)).items():
        key = repository_key(keys) or keys[0]
        repos.append(Repository(key=key, moved=list(keys), target=target))
    records = await _records(db, user_id, project)
    _link_by_recorded_locations(repos, folders, records)
    matched_checkouts = _apply_checkouts(repos, folders, checkouts)
    await _assign_targets(db, user_id, project, repos)

    current = [m for m in records if not m.get("superseded_by")]
    versions: dict[str, list[str]] = {}
    for memory in records:
        session_key = (memory.get("metadata") or {}).get("relay_key")
        if memory.get("superseded_by") and memory.get("memory_type") == "handoff" and isinstance(session_key, str):
            versions.setdefault(session_key, []).append(memory["id"])
    moves: list[dict[str, Any]] = []
    stays: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for memory in current:
        repo, how, detail = _match(memory, repos, checkouts, matched_checkouts, project)
        relay = relay_block(memory) if memory.get("memory_type") == "handoff" else None
        if relay is not None and handoff_location(memory) is None and _commits_of(relay):
            candidates.append({"memory_id": memory["id"], "commits": _commits_of(relay)})
        item = _summary(memory)
        if repo is not None and not repo.keeps:
            relay_key = (memory.get("metadata") or {}).get("relay_key")
            earlier = versions.get(relay_key, []) if isinstance(relay_key, str) else []
            moves.append({**item, "to_project": repo.target, "matched_by": how, "evidence": detail, "versions": earlier})
        elif repo is not None:
            stays.append({**item, "reason": f"belongs to repository {_safe(repo.name, 80)}, which keeps project '{project}'"})
        else:
            stays.append({**item, "reason": detail})
    moving = [r for r in repos if r.fingerprints and not r.keeps]
    targets = {r.target for r in moving} | {str(m["to_project"]) for m in moves}
    restricted = await restricted_credentials(db, user_id, project, targets) if targets else []
    return {
        "project": project,
        "repositories": [r.as_dict() for r in sorted(repos, key=lambda r: (r.already, r.first_seen, r.key))],
        "folders": [_safe_place(_path_parts(k)) for k in folders if k.startswith(f"{KIND_PATH}:")],
        "moves": moves,
        "stays": stays[:MAX_LISTED_STAYS],
        "stays_total": len(stays),
        "commit_candidates": candidates,
        "checkouts": [
            {**c.describe(), "matched": _safe(matched_checkouts[i].name, 120) if i in matched_checkouts else None}
            for i, c in enumerate(checkouts)
        ],
        "bindings_to_move": sum(len(r.fingerprints) for r in moving),
        "restricted_credentials": restricted,
        "applied": False,
        "batch_id": None,
        "_repos": moving,  # for apply(); removed before the result leaves the service
    }


async def _optional_rows(db: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
    """Rows of a query on a table a deployment may not have (RBAC roles, OAuth grants): [] without it."""
    try:
        cursor = await db.conn.execute(sql, params)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise
    return list(await cursor.fetchall())


async def restricted_credentials(db: Any, user_id: str, project: str, targets: set[str]) -> list[dict[str, Any]]:
    """The account's API keys and connections restricted to ``project`` that would lose ``targets``.

    A key restricted to projects is refused in every project it does not name.
    Before the split the repositories were in ``project``, so such a key could
    brief and close in them; after it they are in their own projects, and its
    closes there are refused (and dropped from the client's queue after 14
    days). Each entry: ``{kind, id, name, project_ids, loses}`` (ids and names
    only, never a secret).
    """
    out: list[dict[str, Any]] = []
    keys = await _optional_rows(
        db,
        "SELECT k.id, k.name, r.project_ids FROM api_keys k JOIN api_key_roles r ON r.api_key_id = k.id "
        "WHERE k.user_id = ? AND k.active AND COALESCE(r.project_ids, '') != '' ORDER BY k.created_at, k.id",
        (user_id,),
    )
    for key_id, name, projects in keys:
        allowed = [p for p in str(projects or "").split(",") if p]
        loses = sorted(targets - set(allowed))
        if project in allowed and loses:
            out.append({"kind": "api_key", "id": str(key_id), "name": _safe(name, 80), "project_ids": allowed, "loses": loses})
    grants = await _optional_rows(
        db,
        "SELECT g.grant_id, COALESCE(c.client_name, g.client_id), g.project_ids FROM oauth_grants g "
        "LEFT JOIN oauth_clients c ON c.client_id = g.client_id WHERE g.user_id = ? AND g.revoked_at IS NULL "
        "ORDER BY g.created_at, g.grant_id",
        (user_id,),
    )
    for grant_id, name, projects in grants:
        try:
            allowed = [str(p) for p in json.loads(projects or "[]")]
        except (TypeError, ValueError):
            continue
        loses = sorted(targets - set(allowed))
        if project in allowed and loses:
            out.append(
                {"kind": "connection", "id": str(grant_id), "name": _safe(name, 80), "project_ids": allowed, "loses": loses}
            )
    return out


class SplitNeedsConfirmation(Exception):
    """Applying would take repositories away from keys restricted to the project; the caller must confirm."""

    def __init__(self, project: str, credentials: list[dict[str, Any]]) -> None:
        names = ", ".join(f"{c['kind'].replace('_', ' ')} {c.get('name') or c['id']}" for c in credentials)
        super().__init__(
            f"Needs --keys-lose-access (API: restricted_keys_lose_access): {len(credentials)} API key(s) or "
            f"connection(s) restricted to '{project}' would be refused in the split repositories. Or add the new "
            f"projects to them first (dashboard). They are: {names}."
        )
        self.credentials = credentials


async def _move_memory(db: Any, user_id: str, memory_id: str, source: str, target: str, now: str) -> bool:
    cursor = await db.conn.execute(
        "UPDATE memories SET project_id = ?, updated_at = ? WHERE id = ? AND user_id = ? AND project_id = ?",
        (target, now, memory_id, user_id, source),
    )
    if (cursor.rowcount or 0) <= 0:
        return False
    await db.conn.execute("UPDATE memories_fts SET project_id = ? WHERE id = ? AND user_id = ?", (target, memory_id, user_id))
    await db.conn.execute(
        "UPDATE pending_embeddings SET project_id = ? WHERE memory_id = ? AND user_id = ?", (target, memory_id, user_id)
    )
    # Who picked the handoff up goes with it (deleting a project removes its pickups by project id).
    await db.conn.execute(
        "UPDATE relay_pickups SET project_id = ? WHERE user_id = ? AND handoff_id = ?", (target, user_id, memory_id)
    )
    return True


async def _log(db: Any, user_id: str, batch: str, kind: str, ref: str, source: str, target: str, evidence: str, now: str) -> None:
    await db.conn.execute(
        "INSERT INTO relay_refiles (user_id, batch_id, kind, ref, from_project, to_project, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, batch, kind, ref, source, target, evidence[:500], now),
    )


async def _sync_vectors(qdrant: Any, moved: list[tuple[str, str]]) -> int:
    """Point each moved memory's vector payload at its new project. Returns how many failed (logged)."""
    setter = getattr(qdrant, "set_payload_fields", None)
    if setter is None or not moved:
        return 0
    existing = getattr(qdrant, "existing_ids", None)
    if existing is not None:
        try:
            # A memory whose vector is not written yet gets its payload from the (moved) row later.
            have = await existing([memory_id for memory_id, _ in moved])
            moved = [(memory_id, project) for memory_id, project in moved if memory_id in have]
        except Exception as e:
            log.warning("relay_split_vector_lookup_failed", error=str(e))
    failed = 0
    for memory_id, project in moved:
        try:
            await setter(memory_id, {"project_id": project})
        except Exception as e:  # SQLite is the source of truth; recall re-checks every hit against it
            failed += 1
            log.warning("relay_split_vector_payload_failed", memory_id=memory_id, error=str(e))
    return failed


async def apply(
    db: Any,
    user_id: str,
    project: str,
    checkouts: list[Checkout] | None = None,
    qdrant: Any | None = None,
    *,
    restricted_keys_lose_access: bool = False,
) -> dict[str, Any]:
    """Carry out :func:`plan` and log every change under a new batch id.

    The plan is made inside the same transaction as the moves, so nothing
    written in between can be missed. Raises :class:`SplitNeedsConfirmation`
    when keys or connections restricted to ``project`` would lose repositories
    and ``restricted_keys_lose_access`` is not set (nothing changes). A target
    project the batch creates is logged (kind ``project``) so :func:`undo` can
    take back everything of that repository. After the moves, a session left
    with more than one current handoff in a project (one moved in beside
    another) keeps only its newest, as a close would (logged as ``supersede``).
    Callers hold :func:`user_gate` exclusively so no close of the account runs
    meanwhile.
    """
    await ensure_schema(db)
    batch = f"split-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    now = _now()
    bindings = 0
    memories: list[tuple[str, str]] = []
    async with db.transaction():
        result = await plan(db, user_id, project, checkouts)
        repos: list[Repository] = result.pop("_repos")
        credentials = result["restricted_credentials"]
        if credentials and not restricted_keys_lose_access and (repos or result["moves"]):
            raise SplitNeedsConfirmation(project, credentials)
        for repo in repos:
            moved_here = 0
            for key in repo.fingerprints:
                cursor = await db.conn.execute(
                    "UPDATE project_fingerprints SET project_id = ?, updated_at = ? "
                    "WHERE user_id = ? AND fingerprint = ? AND project_id = ?",
                    (repo.target, now, user_id, key, project),
                )
                if (cursor.rowcount or 0) > 0:
                    moved_here += 1
                    await _log(db, user_id, batch, "binding", key, project, repo.target, f"repository {repo.key}", now)
            bindings += moved_here
            if moved_here and not repo.already:
                evidence = f"created by this split for repository {repo.key}"
                await _log(db, user_id, batch, KIND_PROJECT, repo.target, project, repo.target, evidence, now)
        for move in result["moves"]:
            for memory_id in [move["memory_id"], *move["versions"]]:
                if await _move_memory(db, user_id, memory_id, project, move["to_project"], now):
                    memories.append((memory_id, move["to_project"]))
                    await _log(db, user_id, batch, "memory", memory_id, project, move["to_project"], move["evidence"], now)
        superseded = await _one_current_per_session(db, user_id, batch, [m for m, _ in memories], now)
    vector_errors = await _sync_vectors(qdrant, memories) if qdrant is not None else 0
    applied = bool(bindings or memories)
    log.info(
        "relay_project_split",
        project=project,
        batch=batch if applied else None,
        bindings=bindings,
        memories=len(memories),
        superseded=superseded,
    )
    result.update(
        applied=applied,
        batch_id=batch if applied else None,
        moved={
            "bindings": bindings,
            "memories": len(memories),
            "superseded": superseded,
            "vector_payload_errors": vector_errors,
        },
    )
    return result


def _active_cutoff() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


async def _one_current_per_session(db: Any, user_id: str, batch: str, moved_ids: list[str], now: str) -> int:
    """Supersede all but the newest current handoff of each moved session in its new project.

    A session can have a current handoff in two projects (it closed in a
    folder and in the repository, or a close raced an earlier split); once
    both are in one project, the older one is retired the way a repeat close
    retires it. Returns how many were superseded (each logged)."""
    if not moved_ids:
        return 0
    marks = ",".join("?" for _ in moved_ids)
    cursor = await db.conn.execute(
        f"SELECT DISTINCT project_id, json_extract(metadata, '$.relay_key') FROM memories "  # noqa: S608
        f"WHERE user_id = ? AND id IN ({marks}) AND memory_type = 'handoff' AND json_valid(metadata)",
        [user_id, *moved_ids],
    )
    count = 0
    for target, key in await cursor.fetchall():
        if not isinstance(key, str) or not key:
            continue
        rows = await db.conn.execute(
            f"""
            SELECT id FROM memories
            WHERE user_id = ? AND project_id = ? AND memory_type = 'handoff' AND superseded_by IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
              AND json_valid(metadata) AND json_extract(metadata, '$.relay_key') = ?
            ORDER BY {HANDOFF_ENDED_JD} DESC, julianday(created_at) DESC, id DESC
            """,  # noqa: S608 - a fixed SQL expression; values are bound
            (user_id, target, _active_cutoff(), key),
        )
        current = [str(r[0]) for r in await rows.fetchall()]
        for older in current[1:]:
            await db.mark_memory_superseded(older, current[0], now)
            evidence = f"superseded by {current[0]}: one current handoff per session"
            await _log(db, user_id, batch, KIND_SUPERSEDE, older, str(target), str(target), evidence, now)
            count += 1
    return count


async def _batch_rows(db: Any, user_id: str, batch_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    if batch_id:
        chosen = batch_id
    else:
        cursor = await db.conn.execute(
            "SELECT batch_id FROM relay_refiles WHERE user_id = ? AND undone_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise SplitRefused("There is no split to undo.")
        chosen = str(row[0])
    cursor = await db.conn.execute(
        "SELECT id, kind, ref, from_project, to_project, evidence, created_at, undone_at FROM relay_refiles "
        "WHERE user_id = ? AND batch_id = ? ORDER BY id",
        (user_id, chosen),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    if not rows:
        raise SplitRefused(f"No split batch '{chosen}' on this account.")
    return chosen, rows


async def _written_since(
    db: Any, user_id: str, target: str, source: str, batch_refs: set[str], repo_keys: set[str]
) -> list[dict[str, Any]]:
    """What undo takes back from ``target``, a project the batch created for one repository:
    every location bound there since the split (it resolves to that repository's project, so it is
    that repository's: another checkout or worktree), and every handoff whose recorded location is
    one of the repository's (every version). Nothing else: checkpoints and handoffs with no recorded
    location stay (see ``written_since``)."""
    items: list[dict[str, Any]] = []
    cursor = await db.conn.execute(
        "SELECT fingerprint FROM project_fingerprints WHERE user_id = ? AND project_id = ? ORDER BY created_at, fingerprint",
        (user_id, target),
    )
    keys = set(repo_keys)
    for (key,) in await cursor.fetchall():
        if str(key) in batch_refs:
            continue
        keys.add(str(key))
        items.append({"kind": "binding", "ref": str(key), "from_project": target, "to_project": source, "since_split": True})
    cursor = await db.conn.execute(
        "SELECT id, memory_type, metadata, source FROM memories WHERE user_id = ? AND project_id = ? "
        "AND memory_type = 'handoff' ORDER BY julianday(created_at), id",
        (user_id, target),
    )
    for row in await cursor.fetchall():
        memory = {"id": str(row[0]), "memory_type": row[1], "metadata": _parse_metadata(row[2]), "source": row[3]}
        if memory["id"] in batch_refs:
            continue
        recorded = {str(k) for k in (handoff_location(memory) or {}).get("fingerprints") or []}
        if recorded & keys:
            items.append(
                {"kind": "memory", "ref": memory["id"], "from_project": target, "to_project": source, "since_split": True}
            )
    return items


async def _undo_plan(db: Any, user_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pending = [r for r in rows if not r["undone_at"]]
    moves = [r for r in pending if r["kind"] in MOVE_KINDS]
    bound = await _bound_to(db, user_id, [r["ref"] for r in moves if r["kind"] == "binding"])
    memory_ids = [r["ref"] for r in moves if r["kind"] == "memory"]
    located: dict[str, str] = {}
    if memory_ids:
        marks = ",".join("?" for _ in memory_ids)
        cursor = await db.conn.execute(
            f"SELECT id, project_id FROM memories WHERE user_id = ? AND id IN ({marks})",  # noqa: S608
            [user_id, *memory_ids],
        )
        located = {str(r[0]): str(r[1]) for r in await cursor.fetchall()}
    back: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    for r in moves:
        now_at = bound.get(r["ref"]) if r["kind"] == "binding" else located.get(r["ref"])
        item = {"kind": r["kind"], "ref": r["ref"], "from_project": r["to_project"], "to_project": r["from_project"]}
        if now_at == r["to_project"]:
            back.append(item)
        else:
            left.append({**item, "reason": f"now in '{now_at}'" if now_at else "no longer exists"})
    batch_refs = {r["ref"] for r in rows if r["kind"] in MOVE_KINDS}
    since: list[dict[str, Any]] = []
    for r in pending:
        if r["kind"] == KIND_PROJECT:
            repo_keys = {m["ref"] for m in moves if m["kind"] == "binding" and m["to_project"] == r["ref"]}
            since.extend(await _written_since(db, user_id, r["ref"], r["from_project"], batch_refs, repo_keys))
    # Anything else written in a split-out project since the split is not the batch's: it stays there.
    taken = batch_refs | {i["ref"] for i in since}
    written_since: dict[str, int] = {}
    for target in sorted({r["to_project"] for r in moves}):
        cursor = await db.conn.execute(
            "SELECT id FROM memories WHERE user_id = ? AND project_id = ? AND memory_type IN ('handoff', 'checkpoint') "
            "AND superseded_by IS NULL",
            (user_id, target),
        )
        count = sum(1 for r in await cursor.fetchall() if str(r[0]) not in taken)
        if count:
            written_since[target] = count
    return {"pending": bool(pending), "back": back, "since": since, "left": left, "written_since": written_since}


async def undo(
    db: Any, user_id: str, batch_id: str | None = None, *, apply_it: bool = False, qdrant: Any = None
) -> dict[str, Any]:
    """Reverse one split batch (the latest one not undone by default).

    A binding goes back only while it is still bound where the split put it,
    and a memory only while it is still in that project; anything changed since
    is listed and left alone. A project the batch created for a repository goes
    back whole: the locations of that repository bound there since (another
    checkout or worktree) and the handoffs recorded in them go back too
    (``since_split`` in ``moves_back``, logged in the batch), so the repository
    resolves to one project again and a later split can use the same name.
    Checkpoints and handoffs with no recorded location written there since stay
    and are counted in ``written_since``. A session left with two current
    handoffs in one project keeps its newest (``superseded`` in ``moved``, logged
    in the batch). Dry run unless ``apply_it``. Undoing a
    batch twice changes nothing the second time. Callers hold :func:`user_gate`
    exclusively when applying.
    """
    await ensure_schema(db)
    batch, rows = await _batch_rows(db, user_id, batch_id)
    if not apply_it:
        found = await _undo_plan(db, user_id, rows)
        return {
            "batch_id": batch,
            "already_undone": not found["pending"],
            "moves_back": _shown([*found["back"], *found["since"]]),
            "left": _shown(found["left"]),
            "written_since": found["written_since"],
            "applied": False,
        }
    now = _now()
    moved: list[tuple[str, str]] = []
    bindings = 0
    async with db.transaction():
        found = await _undo_plan(db, user_id, rows)
        result: dict[str, Any] = {
            "batch_id": batch,
            "already_undone": not found["pending"],
            "moves_back": [*found["back"], *found["since"]],
            "left": found["left"],
            "written_since": found["written_since"],
            "applied": False,
        }
        if not found["pending"]:
            return result
        for item in result["moves_back"]:
            if item["kind"] == "binding":
                cursor = await db.conn.execute(
                    "UPDATE project_fingerprints SET project_id = ?, updated_at = ? "
                    "WHERE user_id = ? AND fingerprint = ? AND project_id = ?",
                    (item["to_project"], now, user_id, item["ref"], item["from_project"]),
                )
                done = (cursor.rowcount or 0) > 0
                bindings += 1 if done else 0
            else:
                done = await _move_memory(db, user_id, item["ref"], item["from_project"], item["to_project"], now)
                if done:
                    moved.append((item["ref"], item["to_project"]))
            if done and item.get("since_split"):
                evidence = f"written in '{item['from_project']}' after the split; moved back by its undo"
                await _log(db, user_id, batch, item["kind"], item["ref"], item["to_project"], item["from_project"], evidence, now)
        # Back in one project, a session keeps one current handoff (as after a split).
        superseded = await _one_current_per_session(db, user_id, batch, [m for m, _ in moved], now)
        await db.conn.execute(
            "UPDATE relay_refiles SET undone_at = ? WHERE user_id = ? AND batch_id = ? AND undone_at IS NULL",
            (now, user_id, batch),
        )
    vector_errors = await _sync_vectors(qdrant, moved) if qdrant is not None else 0
    log.info("relay_project_split_undone", batch=batch, bindings=bindings, memories=len(moved), superseded=superseded)
    result.update(
        applied=True,
        moves_back=_shown(result["moves_back"]),
        left=_shown(result["left"]),
        moved={
            "bindings": bindings,
            "memories": len(moved),
            "superseded": superseded,
            "vector_payload_errors": vector_errors,
        },
    )
    return result


def _shown(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Undo items as returned: a binding's key (a path, a remote) under the trust policy (:func:`_safe_key`)."""
    return [{**item, "ref": _safe_key(item["ref"])} if item.get("kind") == "binding" else item for item in items]


def checkouts_from(items: list[dict[str, Any]]) -> list[Checkout]:
    """Checkouts from the request body (locator fields + the recorded commits the client found in it)."""
    out: list[Checkout] = []
    for item in items:
        locator = ProjectLocator(
            git_remote=item.get("git_remote"),
            root_commit=item.get("root_commit"),
            root_path=item.get("root_path"),
            repo_name=item.get("repo_name"),
            host=item.get("host"),
        )
        if locator.is_empty():
            continue
        present = {str(s).strip().lower() for s in item.get("present") or [] if _SHA_RE.match(str(s).strip().lower())}
        out.append(Checkout(locator=locator, present=present))
    return out
