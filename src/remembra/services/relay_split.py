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
  git that the HEAD or a commit the handoff recorded exists in a checkout of
  exactly one of the repositories. Earlier versions of a moved handoff (same
  agent and session) go with it. Everything else stays where it is and is
  listed with the reason; checkpoints record no location and stay.
* **Safe.** Dry run by default. Applying records every change in
  ``relay_refiles`` (batch, kind, what, from, to, evidence) and one audit
  event, and :func:`undo` reverses a batch. Applying again moves only what is
  still there and matched, so it is idempotent. Nothing is deleted. Every
  query is scoped to the caller's user id.

The ``relay_refiles`` table is created on first use (like the security state
tables), so no schema migration is involved.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.relay.handoff import handoff_headline, handoff_location, handoff_verdict, relay_block, show_text
from remembra.relay.identity import (
    KIND_GIT,
    KIND_PATH,
    KIND_ROOT,
    ProjectLocator,
    repository_key,
    slugify_project,
)
from remembra.services.agent_session import _parse_metadata

log = structlog.get_logger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_LISTED_STAYS = 500
MAX_COMMITS_PER_HANDOFF = 31  # the HEAD and the (at most 30) commits a handoff's relay block keeps

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    @property
    def already(self) -> bool:
        """Split out by an earlier batch (not undone)."""
        return bool(self.moved)

    @property
    def name(self) -> str | None:
        return _repository_name(self.key, [*self.fingerprints, *self.moved])

    def all_keys(self) -> set[str]:
        return {self.key, *self.fingerprints, *self.moved}

    def as_dict(self) -> dict[str, Any]:
        paths = [_path_parts(p) for p in [*self.fingerprints, *self.moved] if p.startswith(f"{KIND_PATH}:")]
        return {
            "key": self.key,
            "name": self.name,
            "target_project": self.target,
            "bindings": sorted(self.fingerprints),
            "already_split": self.already,
            "keeps_project": self.keeps,
            "paths": paths,
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
        return {"root_path": self.locator.root_path, "host": self.locator.host, "repository": repository_key(self.keys)}


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


def _commits_of(relay: dict[str, Any]) -> list[str]:
    shas = [relay.get("head_commit")] + [c.get("sha") for c in relay.get("commits") or [] if isinstance(c, dict)]
    out: list[str] = []
    for sha in shas:
        value = str(sha or "").strip().lower()
        if _SHA_RE.match(value) and value not in out:
            out.append(value)
    return out[:MAX_COMMITS_PER_HANDOFF]


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
    meta = memory.get("metadata") or {}
    relay = relay_block(memory) or {}
    verdict = handoff_verdict(memory)
    headline = "withheld (low trust)" if verdict.withheld else show_text(handoff_headline(memory), verdict, 120)
    location = handoff_location(memory)
    return {
        "memory_id": memory["id"],
        "memory_type": memory.get("memory_type"),
        "agent_id": relay.get("agent_id") or meta.get("agent_id"),
        "session_id": meta.get("session_id"),
        "created_at": memory.get("created_at"),
        "branch": relay.get("branch"),
        "head_commit": relay.get("head_commit"),
        "headline": headline,
        "location": {k: location.get(k) for k in ("name", "root_path", "host", "repository")} if location else None,
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
            return None, "", f"worked in a folder that is not a git repository; folders stay in '{project}'"
        found = [r for r in repos if r.all_keys() & {k for k in keys if not k.startswith(f"{KIND_PATH}:")}]
        if len(found) == 1:
            return found[0], "location", f"recorded location {repo_key}"
        if not found:
            return None, "", f"its recorded repository ({repo_key}) is not bound to '{project}'"
        return None, "", f"its recorded location matches {len(found)} repositories"
    shas = _commits_of(relay)
    if not shas:
        return None, "", "no location or commit recorded (closed before Remembra 0.16.1, outside git)"
    hits: dict[str, tuple[Repository, str, dict[str, Any]]] = {}
    for index, checkout in enumerate(checkouts):
        repo = matched_checkouts.get(index)
        if repo is None:
            continue
        sha = next((s for s in shas if s in checkout.present), None)
        if sha is not None and repo.key not in hits:
            hits[repo.key] = (repo, sha, checkout.describe())
    if len(hits) == 1:
        repo, sha, where = next(iter(hits.values()))
        on = f" on {where['host']}" if where.get("host") else ""
        return repo, "commit", f"commit {sha[:7]} it recorded is in the checkout at {where['root_path']}{on} (read with git)"
    if len(hits) > 1:
        names = ", ".join(sorted(str(r.name or r.key) for r, _, _ in hits.values()))
        return None, "", f"its commits are in more than one repository ({names})"
    if checkouts:
        return None, "", "no location recorded (closed before Remembra 0.16.1), and none of its commits is in a checkout read"
    return None, "", "no location recorded (closed before Remembra 0.16.1); run the split where the repository is checked out"


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
            stays.append({**item, "reason": f"belongs to repository {repo.name}, which keeps project '{project}'"})
        else:
            stays.append({**item, "reason": detail})
    moving = [r for r in repos if r.fingerprints and not r.keeps]
    return {
        "project": project,
        "repositories": [r.as_dict() for r in sorted(repos, key=lambda r: (r.already, r.first_seen, r.key))],
        "folders": [_path_parts(k) for k in folders if k.startswith(f"{KIND_PATH}:")],
        "moves": moves,
        "stays": stays[:MAX_LISTED_STAYS],
        "stays_total": len(stays),
        "commit_candidates": candidates,
        "checkouts": [
            {**c.describe(), "matched": matched_checkouts[i].name if i in matched_checkouts else None}
            for i, c in enumerate(checkouts)
        ],
        "bindings_to_move": sum(len(r.fingerprints) for r in moving),
        "applied": False,
        "batch_id": None,
        "_repos": moving,  # for apply(); removed before the result leaves the service
    }


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
) -> dict[str, Any]:
    """Carry out :func:`plan` in one transaction and log every change under a new batch id."""
    result = await plan(db, user_id, project, checkouts)
    repos: list[Repository] = result.pop("_repos")
    batch = f"split-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    now = _now()
    bindings = 0
    memories: list[tuple[str, str]] = []
    async with db.transaction():
        for repo in repos:
            for key in repo.fingerprints:
                cursor = await db.conn.execute(
                    "UPDATE project_fingerprints SET project_id = ?, updated_at = ? "
                    "WHERE user_id = ? AND fingerprint = ? AND project_id = ?",
                    (repo.target, now, user_id, key, project),
                )
                if (cursor.rowcount or 0) > 0:
                    bindings += 1
                    await _log(db, user_id, batch, "binding", key, project, repo.target, f"repository {repo.key}", now)
        for move in result["moves"]:
            for memory_id in [move["memory_id"], *move["versions"]]:
                if await _move_memory(db, user_id, memory_id, project, move["to_project"], now):
                    memories.append((memory_id, move["to_project"]))
                    await _log(db, user_id, batch, "memory", memory_id, project, move["to_project"], move["evidence"], now)
    vector_errors = await _sync_vectors(qdrant, memories) if qdrant is not None else 0
    applied = bool(bindings or memories)
    log.info("relay_project_split", project=project, batch=batch if applied else None, bindings=bindings, memories=len(memories))
    result.update(
        applied=applied,
        batch_id=batch if applied else None,
        moved={"bindings": bindings, "memories": len(memories), "vector_payload_errors": vector_errors},
    )
    return result


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


async def undo(
    db: Any, user_id: str, batch_id: str | None = None, *, apply_it: bool = False, qdrant: Any = None
) -> dict[str, Any]:
    """Reverse one split batch (the latest one not undone by default).

    A binding goes back only while it is still bound where the split put it,
    and a memory only while it is still in that project; anything changed since
    is listed and left alone. Dry run unless ``apply_it``. Undoing a batch twice
    changes nothing the second time.
    """
    await ensure_schema(db)
    batch, rows = await _batch_rows(db, user_id, batch_id)
    pending = [r for r in rows if not r["undone_at"]]
    bound = await _bound_to(db, user_id, [r["ref"] for r in pending if r["kind"] == "binding"])
    memory_ids = [r["ref"] for r in pending if r["kind"] == "memory"]
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
    for r in pending:
        now_at = bound.get(r["ref"]) if r["kind"] == "binding" else located.get(r["ref"])
        item = {"kind": r["kind"], "ref": r["ref"], "from_project": r["to_project"], "to_project": r["from_project"]}
        if now_at == r["to_project"]:
            back.append(item)
        else:
            left.append({**item, "reason": f"now in '{now_at}'" if now_at else "no longer exists"})
    # Handoffs written in a split-out project since the split are not the batch's: they stay there.
    batch_refs = {r["ref"] for r in rows if r["kind"] == "memory"}
    written_since: dict[str, int] = {}
    for target in sorted({r["to_project"] for r in pending}):
        cursor = await db.conn.execute(
            "SELECT id FROM memories WHERE user_id = ? AND project_id = ? AND memory_type IN ('handoff', 'checkpoint') "
            "AND superseded_by IS NULL",
            (user_id, target),
        )
        count = sum(1 for r in await cursor.fetchall() if str(r[0]) not in batch_refs)
        if count:
            written_since[target] = count
    result: dict[str, Any] = {
        "batch_id": batch,
        "already_undone": not pending,
        "moves_back": back,
        "left": left,
        "written_since": written_since,
        "applied": False,
    }
    if not apply_it or not pending:
        return result
    now = _now()
    moved: list[tuple[str, str]] = []
    bindings = 0
    async with db.transaction():
        for item in back:
            if item["kind"] == "binding":
                cursor = await db.conn.execute(
                    "UPDATE project_fingerprints SET project_id = ?, updated_at = ? "
                    "WHERE user_id = ? AND fingerprint = ? AND project_id = ?",
                    (item["to_project"], now, user_id, item["ref"], item["from_project"]),
                )
                bindings += 1 if (cursor.rowcount or 0) > 0 else 0
            elif await _move_memory(db, user_id, item["ref"], item["from_project"], item["to_project"], now):
                moved.append((item["ref"], item["to_project"]))
        await db.conn.execute(
            "UPDATE relay_refiles SET undone_at = ? WHERE user_id = ? AND batch_id = ? AND undone_at IS NULL",
            (now, user_id, batch),
        )
    vector_errors = await _sync_vectors(qdrant, moved) if qdrant is not None else 0
    log.info("relay_project_split_undone", batch=batch, bindings=bindings, memories=len(moved))
    result.update(applied=True, moved={"bindings": bindings, "memories": len(moved), "vector_payload_errors": vector_errors})
    return result


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
