"""Relay services: project identity, cross-project links, session close-out, trail.

* :class:`ProjectRegistry` maps location-independent fingerprints (git remote /
  root commit / path, see :mod:`remembra.relay.identity`) to a per-user
  project id, and stores typed links between projects.
* :class:`RelayService` turns a session's facts into ONE structured handoff
  memory (idempotent per ``(agent_id, session_id)``: a repeat close supersedes
  the previous version), upserts ``last_agent:<project>`` / ``branch:<project>``
  status keys, lists the cross-agent trail, and builds the pickup brief.

All queries are scoped to the authenticated ``user_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from remembra.client.project import normalize_project_id
from remembra.core.time import utcnow
from remembra.models.memory import StoreRequest
from remembra.relay.handoff import (
    HANDOFF_FORMAT_VERSION,
    assess_handoff,
    build_sections,
    check_summary_grounding,
    handoff_headline,
    handoff_stored_trust,
    police_brief,
    redact,
    render_brief,
    render_handoff,
    stored_health,
)
from remembra.relay.identity import KIND_GIT, KIND_PATH, KIND_ROOT, Fingerprint, ProjectLocator, slugify_project
from remembra.security.untrusted import repo_url_prefixes
from remembra.services.agent_session import AgentSessionService, _parse_metadata

log = structlog.get_logger(__name__)

RELAY_KEY_FIELD = "relay_key"
MAX_LINKED_IN_BRIEF = 8
FACTS_SOURCES = ("relay-cli:git+transcript", "relay-cli:git", "agent-declared")

# Keys only the server may write into a memory's metadata. Client metadata on
# the generic memory endpoints (store, batch, PATCH, supersede, status upsert,
# import) is stripped of them (see :func:`strip_reserved_metadata`), so a
# handoff cannot be forged with a "verified" relay block, and the trust fields
# ``health``, ``quality`` and ``confidence`` are only ever server-computed
# (never self-declared by an agent).
SERVER_TRUST_FIELDS = ("health", "quality", "confidence")
RESERVED_METADATA_KEYS = ("relay", RELAY_KEY_FIELD, *SERVER_TRUST_FIELDS)

# Serialises close-outs for the same (user, project, agent+session) in this
# process, so two concurrent closes of one session can't each supersede the
# other (leaving no current handoff). Keyed rather than global: the lock is
# held across memory_service.store (an embedding call with a budget of tens of
# seconds), and one slow store must not queue every other tenant's close.
_close_locks: weakref.WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = weakref.WeakValueDictionary()


def _close_lock_for(user_id: str, project_id: str, key: str) -> asyncio.Lock:
    lock_key = (user_id, project_id, key)
    lock = _close_locks.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _close_locks[lock_key] = lock
    return lock


def strip_reserved_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """``metadata`` without the server-only keys (a copy; None stays None)."""
    if not metadata:
        return metadata
    if not any(k in metadata for k in RESERVED_METADATA_KEYS):
        return metadata
    return {k: v for k, v in metadata.items() if k not in RESERVED_METADATA_KEYS}


def _now_iso() -> str:
    return utcnow().isoformat()


class ProjectAccessDenied(Exception):
    """Raised when a resolved project is outside the caller's allowed projects."""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"No access to project '{project_id}'.")
        self.project_id = project_id


class BindingNotAllowed(Exception):
    """Raised when a project-restricted key tries to (re)bind a location."""

    def __init__(self) -> None:
        super().__init__(
            "Binding a location to a project needs an unrestricted API key: bindings apply to every key of the account."
        )


def _repo_path(git_value: str) -> str:
    """``owner/repo`` part of a normalized ``host/owner/repo`` remote."""
    return git_value.split("/", 1)[1] if "/" in git_value else git_value


class ProjectRegistry:
    """Fingerprint -> project mapping and project links (SQLite)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def _lookup(self, user_id: str, fingerprints: list[Fingerprint]) -> dict[str, str]:
        if not fingerprints:
            return {}
        keys = [fp.key for fp in fingerprints]
        cursor = await self.db.conn.execute(
            "SELECT fingerprint, project_id FROM project_fingerprints "
            f"WHERE user_id = ? AND fingerprint IN ({','.join('?' for _ in keys)})",
            [user_id, *keys],
        )
        return {r[0]: r[1] for r in await cursor.fetchall()}

    async def _project_known(self, user_id: str, project_id: str) -> bool:
        cursor = await self.db.conn.execute(
            "SELECT 1 FROM project_fingerprints WHERE user_id = ? AND project_id = ? LIMIT 1",
            (user_id, project_id),
        )
        return await cursor.fetchone() is not None

    async def fingerprint_values(self, user_id: str, project_id: str) -> dict[str, list[str]]:
        """Fingerprint values on record for a project, grouped by kind (``git`` values are ``host/owner/repo``)."""
        return await self._kinds_of(user_id, project_id)

    async def _kinds_of(self, user_id: str, project_id: str) -> dict[str, list[str]]:
        """Fingerprint values on record for a project, grouped by kind."""
        cursor = await self.db.conn.execute(
            "SELECT kind, fingerprint FROM project_fingerprints WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
        out: dict[str, list[str]] = {}
        for kind, key in await cursor.fetchall():
            out.setdefault(kind, []).append(str(key).split(":", 1)[1] if ":" in str(key) else str(key))
        return out

    async def _bind(self, user_id: str, fingerprints: list[Fingerprint], project_id: str, overwrite: set[str] | bool) -> None:
        """Record fingerprints for ``project_id``. Keys in ``overwrite`` (or all, when True) move an existing binding."""
        now = _now_iso()
        async with self.db.transaction():
            for fp in fingerprints:
                if overwrite is True or (isinstance(overwrite, set) and fp.key in overwrite):
                    await self.db.conn.execute(
                        """
                        INSERT INTO project_fingerprints (user_id, fingerprint, kind, project_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, fingerprint) DO UPDATE SET project_id = excluded.project_id,
                            updated_at = excluded.updated_at
                        """,
                        (user_id, fp.key, fp.kind, project_id, now, now),
                    )
                else:
                    await self.db.conn.execute(
                        """
                        INSERT OR IGNORE INTO project_fingerprints
                            (user_id, fingerprint, kind, project_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, fp.key, fp.kind, project_id, now, now),
                    )

    async def _derive_new_id(self, user_id: str, locator: ProjectLocator, primary: Fingerprint) -> str:
        base = slugify_project(locator.display_name())
        if not await self._project_known(user_id, base):
            return base
        suffix = hashlib.sha256(primary.key.encode()).hexdigest()[:6]
        return f"{base[:57]}-{suffix}"

    async def _adopt(
        self, user_id: str, primary: Fingerprint, by_kind: dict[str, Fingerprint], known: dict[str, str]
    ) -> str | None:
        """An existing project a weaker fingerprint already names, when it is safe to join.

        * root commit known, remote new: join when the project has no remote on
          record (remote added later) or its remote has the same ``owner/repo``
          path on another host name (an ssh ``Host`` alias or mirror). A fork
          (same root, different owner) does not join.
        * path known, remote/root new: join when that project has no remote and
          no root commit yet (``git init`` in a folder seen before, or the
          first commit of an empty repository).
        """
        root = by_kind.get(KIND_ROOT)
        if primary.kind == KIND_GIT and root is not None and root.key in known:
            candidate = known[root.key]
            remotes = (await self._kinds_of(user_id, candidate)).get(KIND_GIT, [])
            if not remotes or any(_repo_path(r) == _repo_path(primary.value) for r in remotes):
                return candidate
        path = by_kind.get(KIND_PATH)
        if path is not None and path is not primary and path.key in known:
            candidate = known[path.key]
            kinds = await self._kinds_of(user_id, candidate)
            if KIND_GIT not in kinds and KIND_ROOT not in kinds:
                return candidate
        return None

    async def resolve(
        self,
        user_id: str,
        locator: ProjectLocator,
        hint_project: str | None = None,
        bind: bool = False,
        create: bool = True,
        allowed_projects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a location to a stable project id.

        Lookup uses the strongest fingerprint the client sent (git remote, else
        root commit, else path); when that one is new, a weaker one may name an
        existing project (see :meth:`_adopt`).

        ``hint_project`` names the project for a location seen for the first
        time (a hint for an already-bound location is reported in
        ``warnings``, never applied); ``bind=True`` re-binds a known location.
        ``create=False`` computes the answer without writing anything.

        ``allowed_projects`` (a project-restricted key) restricts the result
        AND makes the call read-only: bindings are per user, so a restricted
        key must not be able to claim or move a location for every other key
        of the account. Such a key cannot bind at all
        (:class:`BindingNotAllowed`); a new location resolves to the hint, or
        to the key's only project, without being recorded.
        """
        restricted = bool(allowed_projects)
        if bind and restricted:
            raise BindingNotAllowed()
        if restricted:
            create = False
        fingerprints = locator.fingerprints()
        hint = normalize_project_id(hint_project) if hint_project and hint_project.strip() else None
        if not fingerprints:
            if not hint:
                raise ValueError("Provide git_remote, root_commit or root_path (or a project id).")
            self._check_allowed(hint, allowed_projects)
            return {
                "project_id": hint,
                "created": False,
                "persisted": False,
                "fingerprint": None,
                "kind": None,
                "bound": False,
                "source": "hint",
                "warnings": [],
            }

        primary = fingerprints[0]
        known = await self._lookup(user_id, fingerprints)
        by_kind = {fp.kind: fp for fp in fingerprints}
        warnings: list[str] = []

        project_id: str | None = None
        created = False
        overwrite = False
        source = "known"
        if hint and bind:
            project_id, overwrite, source = hint, True, "bind"
        elif primary.key in known:
            project_id = known[primary.key]
        else:
            project_id = await self._adopt(user_id, primary, by_kind, known)
            source = "adopted"
        if project_id is None:
            if hint:
                project_id, source = hint, "hint"
            elif restricted and allowed_projects and len(allowed_projects) == 1:
                project_id, source = allowed_projects[0], "key-project"
            else:
                project_id, source = await self._derive_new_id(user_id, locator, primary), "derived"
            created = not await self._project_known(user_id, project_id)
        elif hint and hint != project_id and not bind:
            warnings.append(
                f"hint_project '{hint}' was not applied: this location is already bound to project '{project_id}'. "
                f"To move it run `remembra-relay resolve --project {hint} --bind` (or resolve with bind=true)."
            )

        self._check_allowed(project_id, allowed_projects)

        # Register the primary fingerprint, plus the root commit (a later clone
        # without a remote still resolves) and the host-qualified checkout path
        # (callers that only know their working directory, e.g. an MCP client
        # passing root_path, find the same project). The path moves with
        # whatever repository is checked out there now.
        to_bind = [primary]
        if primary.kind == KIND_GIT and KIND_ROOT in by_kind:
            to_bind.append(by_kind[KIND_ROOT])
        path_fp = by_kind.get(KIND_PATH)
        movable: set[str] = set()
        if path_fp is not None and primary.kind != KIND_PATH and locator.host and locator.host.strip():
            to_bind.append(path_fp)
            movable.add(path_fp.key)
        missing = [fp for fp in to_bind if overwrite or known.get(fp.key) != project_id]
        persisted = False
        if create and missing:
            await self._bind(user_id, missing, project_id, overwrite=True if overwrite else movable)
            persisted = True
            log.info("relay_project_bound", project_id=project_id, fingerprints=[fp.kind for fp in missing], rebind=overwrite)
        elif not missing or known.get(primary.key) == project_id:
            persisted = True  # already on record
        return {
            "project_id": project_id,
            "created": created and persisted,
            "persisted": persisted,
            "fingerprint": primary.key,
            "kind": primary.kind,
            "bound": overwrite and persisted,
            "source": source,
            "warnings": warnings,
        }

    @staticmethod
    def _check_allowed(project_id: str, allowed: list[str] | None) -> None:
        if allowed and project_id not in allowed:
            raise ProjectAccessDenied(project_id)

    async def fingerprints_for(self, user_id: str, project_id: str) -> list[dict[str, Any]]:
        cursor = await self.db.conn.execute(
            "SELECT fingerprint, kind, created_at FROM project_fingerprints "
            "WHERE user_id = ? AND project_id = ? ORDER BY created_at",
            (user_id, project_id),
        )
        return [{"fingerprint": r[0], "kind": r[1], "created_at": r[2]} for r in await cursor.fetchall()]

    # -- links ---------------------------------------------------------------

    async def add_link(
        self, user_id: str, from_project: str, to_project: str, relation: str, agent_id: str | None
    ) -> dict[str, Any]:
        if from_project == to_project:
            raise ValueError("A project cannot be linked to itself.")
        now = _now_iso()
        async with self.db.transaction():
            cursor = await self.db.conn.execute(
                """
                INSERT OR IGNORE INTO project_links (user_id, from_project, to_project, relation, created_at, created_by_agent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, from_project, to_project, relation, now, agent_id),
            )
            created = (cursor.rowcount or 0) > 0
        return {"from_project": from_project, "to_project": to_project, "relation": relation, "created": created}

    async def remove_link(self, user_id: str, from_project: str, to_project: str, relation: str) -> bool:
        async with self.db.transaction():
            cursor = await self.db.conn.execute(
                "DELETE FROM project_links WHERE user_id = ? AND from_project = ? AND to_project = ? AND relation = ?",
                (user_id, from_project, to_project, relation),
            )
            return (cursor.rowcount or 0) > 0

    async def links_for(self, user_id: str, project_id: str) -> list[dict[str, Any]]:
        """Links in both directions; ``project_id`` in each item is the OTHER project."""
        cursor = await self.db.conn.execute(
            """
            SELECT from_project, to_project, relation, created_at, created_by_agent FROM project_links
            WHERE user_id = ? AND (from_project = ? OR to_project = ?)
            ORDER BY created_at, from_project, to_project
            """,
            (user_id, project_id, project_id),
        )
        items = []
        for r in await cursor.fetchall():
            outgoing = r[0] == project_id
            items.append(
                {
                    "project_id": r[1] if outgoing else r[0],
                    "relation": r[2],
                    "direction": "outgoing" if outgoing else "incoming",
                    "from_project": r[0],
                    "to_project": r[1],
                    "created_at": r[3],
                    "created_by_agent": r[4],
                }
            )
        return items


def relay_key(agent_id: str, session_id: str) -> str:
    return f"{agent_id}\x1f{session_id}"


class RelayService:
    """Close-out, trail and pickup brief over the memory store."""

    def __init__(self, db: Any, memory_service: Any | None = None) -> None:
        self.db = db
        self.memory_service = memory_service
        self.sessions = AgentSessionService(db=db, memory_service=memory_service)
        self.registry = ProjectRegistry(db)

    async def _current_handoffs_for_key(self, user_id: str, project_id: str, key: str) -> list[dict[str, Any]]:
        cursor = await self.db.conn.execute(
            """
            SELECT id, metadata, content, created_at FROM memories
            WHERE user_id = ? AND project_id = ? AND memory_type = 'handoff' AND superseded_by IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
              AND json_valid(metadata) AND json_extract(metadata, '$.relay_key') = ?
            ORDER BY julianday(created_at) DESC, id DESC
            """,
            (user_id, project_id, _now_iso(), key),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def close_session(
        self,
        *,
        user_id: str,
        project_id: str,
        agent_id: str,
        session_id: str,
        facts: dict[str, Any],
        summary: str | None = None,
        end_reason: str | None = None,
        agent_verified: bool = False,
        screen: Any | None = None,
        scrub: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        """Store (or update) the ONE handoff for ``(agent_id, session_id)``.

        Every input string passes ``redact_secrets`` and then ``scrub`` (the
        API passes the PII policy) before anything is built, so the rendered
        text AND the structured metadata are clean. ``screen`` post-processes
        the rendered text (sanitizer) and returns ``(text, trust_score, checksum)``.
        Returns ``{handoff_id, changed, superseded, rendered, headline, sections,
        grounding, health, redactions, status}``; ``health`` is the server's
        grade (:func:`~remembra.relay.handoff.assess_handoff`), also stored in
        the relay block.
        """
        if self.memory_service is None:
            raise RuntimeError("close_session requires a memory service")
        counts: dict[str, int] = {}
        facts = redact(dict(facts), counts, scrub)
        summary = redact(summary, counts, scrub) if summary else None
        end_reason = redact(end_reason, counts, scrub) if end_reason else None

        closed_at = datetime.now(UTC)
        grounding = check_summary_grounding(summary, facts)
        sections = build_sections(facts, facts.get("next_step"))
        text = render_handoff(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            closed_at=closed_at,
            facts=facts,
            sections=sections,
            summary=summary,
            grounding=grounding,
            end_reason=end_reason,
        )
        trust_score, checksum = 1.0, None
        if screen is not None:
            text, trust_score, checksum = screen(text)

        health = assess_handoff(facts, grounding, trust=float(trust_score))
        key = relay_key(agent_id, session_id)
        facts_source = facts.get("facts_source")
        relay_meta: dict[str, Any] = {
            "v": HANDOFF_FORMAT_VERSION,
            "agent_id": agent_id,
            "agent_verified": agent_verified,
            "session_id": session_id,
            # Who gathered the facts. Declared by the client: the CLI reads git and
            # the transcript itself, an MCP agent types them. Shown, never trusted.
            "facts_source": facts_source if facts_source in FACTS_SOURCES else "agent-declared",
            "commit_evidence": facts.get("commit_evidence"),
            "incomplete": list(facts.get("incomplete") or []),
            # Sanitizer verdict on the rendered text, which carries every free-text
            # field shown in the brief (commit subjects, todos, errors, next, notes).
            "trust_score": round(float(trust_score), 2),
            "branch": facts.get("branch"),
            "head_commit": facts.get("head_commit"),
            "upstream": facts.get("upstream"),
            "unpushed_commits": facts.get("unpushed_commits"),
            "commits": [{"sha": c.get("sha"), "subject": c.get("subject")} for c in (facts.get("commits") or [])[:30]],
            "files_changed_count": len(facts.get("files_changed") or []),
            "uncommitted_count": len(facts.get("uncommitted_files") or []),
            "done": sections["done"],
            "not_done": sections["not_done"],
            "failing": sections["failing"],
            "next": sections["next"],
            "next_source": sections["next_source"],
            "headline": sections["headline"],
            "end_reason": end_reason,
            "grounding": grounding,
            # Server-computed from the facts above (never accepted from a client:
            # "health" is a reserved metadata key on every generic write path).
            "health": health,
            "closed_at": closed_at.isoformat(),
        }
        metadata = {
            RELAY_KEY_FIELD: key,
            "agent_id": agent_id,
            "session_id": session_id,
            "source": "relay",
            "relay": relay_meta,
        }

        async with _close_lock_for(user_id, project_id, key):
            current = await self._current_handoffs_for_key(user_id, project_id, key)
            if len(current) == 1:
                # Identical re-close (hook retried, close called twice): the
                # facts and every section match, only the close time differs.
                prev_meta = _parse_metadata(current[0].get("metadata"))
                raw_relay = prev_meta.get("relay")
                prev_relay: dict[str, Any] = raw_relay if isinstance(raw_relay, dict) else {}
                prev_body = (current[0].get("content") or "").split("\n", 1)[1:]
                if _same_session_facts(prev_relay, relay_meta) and prev_body == text.strip().split("\n", 1)[1:]:
                    return self._close_result(
                        current[0]["id"], False, [], current[0]["content"], sections, grounding, counts, health
                    )
            request = StoreRequest(
                content=text,
                user_id=user_id,
                project_id=project_id,
                memory_type="handoff",
                metadata=metadata,
                skip_extraction=True,
            )
            stored = await self.memory_service.store(
                request, source="agent_generated", trust_score=trust_score, checksum=checksum, skip_extraction=True
            )
            new_id = stored.id
            superseded: list[str] = []
            for row in await self._current_handoffs_for_key(user_id, project_id, key):
                if row["id"] == new_id:
                    continue
                await self.db.mark_memory_superseded(row["id"], new_id)
                superseded.append(row["id"])

        status_updates = []
        where = facts.get("branch") or "(no branch)"
        if facts.get("head_commit"):
            where += f"@{str(facts['head_commit'])[:7]}"
        for status_key, value in (
            (f"last_agent:{project_id}", f"{agent_id} (session {session_id[:40]}) on {where}"),
            (f"branch:{project_id}", where),
        ):
            try:
                res = await self.sessions.upsert_status(
                    user_id=user_id,
                    project_id=project_id,
                    key=status_key,
                    value=value,
                    metadata={"agent_id": agent_id, "session_id": session_id, "source": "relay"},
                    source="agent_generated",
                )
                status_updates.append({"key": res["key"], "value": res["value"], "changed": res["changed"]})
            except Exception as e:  # the handoff is stored; a status hiccup must not fail the close
                log.warning("relay_status_upsert_failed", key=status_key, error=str(e))
        log.info("relay_session_closed", project_id=project_id, agent_id=agent_id, handoff_id=new_id, superseded=len(superseded))
        result = self._close_result(new_id, True, superseded, text, sections, grounding, counts, health)
        result["status"] = status_updates
        return result

    @staticmethod
    def _close_result(
        handoff_id: str,
        changed: bool,
        superseded: list[str],
        text: str,
        sections: dict[str, Any],
        grounding: dict[str, Any],
        counts: dict[str, int],
        health: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "handoff_id": handoff_id,
            "changed": changed,
            "superseded": superseded,
            "rendered": text,
            "headline": sections["headline"],
            "sections": {k: sections[k] for k in ("done", "not_done", "failing", "next")},
            "grounding": grounding,
            "health": health,
            "redactions": counts,
            "status": [],
        }

    async def trail(
        self,
        user_id: str,
        project_id: str | None,
        limit: int = 20,
        offset: int = 0,
        agent_id: str | None = None,
        before: tuple[datetime, str | None] | None = None,
    ) -> dict[str, Any]:
        """Handoffs and checkpoints across agents, newest first.

        ``before`` (``(created_at, id)`` of the oldest entry already shown)
        pages by cursor: the result holds only older entries, and ``total``
        counts the entries older than the cursor.

        Each item carries the headline and counts for a compact list, plus
        ``detail`` (the handoff's Done / Not done / Failing / Next sections,
        commits and grounding) so a reader can expand it without another call.
        Checkpoints and free-form handoffs have no sections; their ``detail``
        holds the stored ``content`` instead.
        """
        result = await self.sessions.timeline(
            user_id=user_id,
            project_id=project_id,
            memory_types=["handoff", "checkpoint"],
            limit=limit,
            offset=offset,
            newest_first=True,
            agent_id=agent_id,
            before=before,
        )
        items = []
        for mem in result["memories"]:
            meta = mem.get("metadata") or {}
            raw_relay = meta.get("relay")
            relay: dict[str, Any] = raw_relay if isinstance(raw_relay, dict) else {}
            items.append(
                {
                    "id": mem["id"],
                    "project_id": mem.get("project_id"),
                    "memory_type": mem.get("memory_type"),
                    "agent_id": mem.get("agent_id") or relay.get("agent_id"),
                    "session_id": meta.get("session_id"),
                    "created_at": mem.get("created_at"),
                    "branch": relay.get("branch") or meta.get("branch"),
                    "head_commit": relay.get("head_commit") or meta.get("head_commit"),
                    "headline": handoff_headline(mem),
                    "failing": len(relay.get("failing") or []),
                    "open": len(relay.get("not_done") or []),
                    "health": stored_health(mem),
                    "detail": _trail_detail(mem, relay),
                }
            )
        out: dict[str, Any] = {"project_id": project_id, "agent_id": agent_id, "items": items, "total": result["total"]}
        # Echo the cursor so a client can tell this server paged by it.
        out["before"] = {"created_at": before[0].isoformat(), "id": before[1]} if before is not None else None
        return out

    async def activity_summary(
        self,
        user_id: str,
        days: int = 14,
        tz_offset_minutes: int = 0,
        allowed: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Per-agent and per-project activity over handoffs and checkpoints.

        ``agents``: every agent id seen, with all-time totals, the last active
        time, sessions (handoffs) in the last 7 days and a ``daily`` series of
        ``days`` counts (oldest first; the last entry is today). Days are local
        to ``tz_offset_minutes`` (minutes east of UTC). ``projects``: the same
        per project. ``week``: handoffs, checkpoints, agents and projects in the
        last 7 days. Only current (not superseded, not expired) rows count, so a
        re-closed session counts once. ``allowed`` restricts to those projects.
        """
        days = max(1, min(int(days), 90))
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        offset = timedelta(minutes=tz_offset_minutes)
        today = (now_utc + offset).date()
        first_day = today - timedelta(days=days - 1)
        week_start = now_utc - timedelta(days=7)
        window_start = min(
            datetime.combine(first_day, datetime.min.time(), tzinfo=UTC) - offset,
            week_start,
        )

        where = (
            "user_id = ? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "AND memory_type IN ('handoff', 'checkpoint')"
        )
        params: list[Any] = [user_id, _now_iso()]
        if allowed:
            where += f" AND project_id IN ({','.join('?' for _ in allowed)})"
            params.extend(allowed)
        agent_expr = "CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.agent_id') END"

        cursor = await self.db.conn.execute(
            f"""
            SELECT {agent_expr} AS agent, project_id,
                   SUM(CASE WHEN memory_type = 'handoff' THEN 1 ELSE 0 END) AS handoffs,
                   SUM(CASE WHEN memory_type = 'checkpoint' THEN 1 ELSE 0 END) AS checkpoints,
                   MAX(julianday(created_at)) AS last_jd
            FROM memories WHERE {where}
            GROUP BY agent, project_id
            """,
            params,
        )
        totals = [dict(r) for r in await cursor.fetchall()]

        cursor = await self.db.conn.execute(
            f"""
            SELECT {agent_expr} AS agent, project_id, memory_type, julianday(created_at) AS jd
            FROM memories WHERE {where} AND julianday(created_at) >= julianday(?)
            """,
            [*params, window_start.replace(tzinfo=None).isoformat()],
        )
        recent = [dict(r) for r in await cursor.fetchall()]

        agents: dict[str, dict[str, Any]] = {}
        projects: dict[str, dict[str, Any]] = {}

        def _agent(name: str) -> dict[str, Any]:
            return agents.setdefault(
                name,
                {
                    "agent_id": name,
                    "handoffs": 0,
                    "checkpoints": 0,
                    "last_active": None,
                    "_last_jd": 0.0,
                    "sessions_7d": 0,
                    "daily": [0] * days,
                    "projects": set(),
                },
            )

        def _project(name: str) -> dict[str, Any]:
            return projects.setdefault(
                name,
                {
                    "project_id": name,
                    "handoffs": 0,
                    "checkpoints": 0,
                    "last_active": None,
                    "_last_jd": 0.0,
                    "sessions_7d": 0,
                    "daily": [0] * days,
                    "agents": set(),
                },
            )

        for row in totals:
            project_name = row["project_id"] or "default"
            buckets = [_project(project_name)]
            if row["agent"]:
                buckets.append(_agent(str(row["agent"])))
                buckets[0]["agents"].add(str(row["agent"]))
                buckets[1]["projects"].add(project_name)
            for bucket in buckets:
                bucket["handoffs"] += int(row["handoffs"] or 0)
                bucket["checkpoints"] += int(row["checkpoints"] or 0)
                if row["last_jd"] is not None and row["last_jd"] > bucket["_last_jd"]:
                    bucket["_last_jd"] = float(row["last_jd"])

        week_counts = {"handoffs": 0, "checkpoints": 0}
        week_agents: set[str] = set()
        week_projects: set[str] = set()
        for row in recent:
            if row["jd"] is None:
                continue
            at = _from_julian(float(row["jd"]))
            project_name = row["project_id"] or "default"
            buckets = [_project(project_name)]
            if row["agent"]:
                buckets.append(_agent(str(row["agent"])))
            index = (days - 1) - (today - (at + offset).date()).days
            in_week = at >= week_start
            is_handoff = row["memory_type"] == "handoff"
            for bucket in buckets:
                if 0 <= index < days:
                    bucket["daily"][index] += 1
                if in_week and is_handoff:
                    bucket["sessions_7d"] += 1
            if in_week:
                week_counts["handoffs" if is_handoff else "checkpoints"] += 1
                week_projects.add(project_name)
                if row["agent"]:
                    week_agents.add(str(row["agent"]))

        def _finish(bucket: dict[str, Any], set_key: str) -> dict[str, Any]:
            last_jd = bucket.pop("_last_jd")
            bucket["last_active"] = _from_julian(last_jd).isoformat() if last_jd else None
            bucket[set_key] = sorted(bucket[set_key])
            return bucket

        agent_list = sorted((_finish(a, "projects") for a in agents.values()), key=lambda a: a["last_active"] or "", reverse=True)
        project_list = sorted(
            (_finish(p, "agents") for p in projects.values()), key=lambda p: p["last_active"] or "", reverse=True
        )
        return {
            "generated_at": now_utc.isoformat(),
            "days": days,
            "tz_offset_minutes": tz_offset_minutes,
            "first_day": first_day.isoformat(),
            "total_handoffs": sum(p["handoffs"] for p in project_list),
            "total_checkpoints": sum(p["checkpoints"] for p in project_list),
            "week": {
                "handoffs": week_counts["handoffs"],
                "checkpoints": week_counts["checkpoints"],
                "agents": sorted(week_agents),
                "projects": sorted(week_projects),
            },
            "agents": agent_list,
            "projects": project_list,
        }

    async def _latest_summary(self, user_id: str, project_id: str) -> dict[str, Any] | None:
        latest = await self.sessions.latest_handoff(user_id, project_id)
        if not latest:
            return None
        return {
            "id": latest["id"],
            "agent_id": latest.get("agent_id"),
            "created_at": latest.get("created_at"),
            "headline": handoff_headline(latest),
            "trust_score": handoff_stored_trust(latest),
        }

    async def linked_with_headlines(self, user_id: str, project_id: str, allowed: list[str] | None) -> list[dict[str, Any]]:
        linked = []
        seen: set[tuple[str, str]] = set()
        for link in await self.registry.links_for(user_id, project_id):
            other = link["project_id"]
            if allowed and other not in allowed:
                continue  # a project-scoped key must not read other projects' handoffs
            if (other, link["relation"]) in seen:
                continue
            seen.add((other, link["relation"]))
            link = dict(link)
            link["latest_handoff"] = await self._latest_summary(user_id, other)
            linked.append(link)
            if len(linked) >= MAX_LINKED_IN_BRIEF:
                break
        return linked

    async def brief(
        self,
        user_id: str,
        project_id: str | None,
        agent_id: str | None,
        recent_n: int = 10,
        inbox_limit: int = 10,
        allowed: list[str] | None = None,
        configured_project: str | None = None,
        checkout: dict[str, Any] | None = None,
        extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """The session brief plus linked projects and a compact rendered text.

        ``configured_project`` is the project the client is configured for
        (e.g. ``REMEMBRA_PROJECT``). When the location resolved to a different
        project, the brief says so and lists the configured project's latest
        handoff, so memories stored there are not silently out of view.
        ``checkout`` (``{branch, head_commit}`` of the reader) marks the
        handoff's failing/next items as possibly stale when it differs.
        """
        brief = await self.sessions.brief(
            user_id=user_id,
            project_id=project_id,
            agent_id=agent_id,
            recent_n=recent_n,
            inbox_limit=inbox_limit,
            # Project-restricted callers (scoped keys, connector grants) only see
            # inbox messages tagged with a project they may access.
            inbox_project_ids=list(allowed) if allowed else None,
        )
        linked = await self.linked_with_headlines(user_id, project_id, allowed) if project_id else []
        warnings: list[str] = list(brief.get("warnings") or [])
        warnings.extend(extra_warnings or [])
        if (
            configured_project
            and project_id
            and configured_project != project_id
            and (not allowed or configured_project in allowed)
        ):
            warnings.append(
                f"This location resolves to project '{project_id}', but this client is configured for "
                f"'{configured_project}'; the configured project's latest handoff is listed under Linked projects. "
                f"To use one project run `remembra-relay resolve --project {configured_project} --bind`."
            )
            if not any(link["project_id"] == configured_project for link in linked):
                linked.insert(
                    0,
                    {
                        "project_id": configured_project,
                        "relation": "configured",
                        "direction": "configured",
                        "latest_handoff": await self._latest_summary(user_id, configured_project),
                    },
                )
        brief["warnings"] = warnings
        brief["linked_projects"] = linked
        brief["checkout"] = checkout
        # URLs into the project's own repository are not flagged in the brief.
        remotes = (await self.registry.fingerprint_values(user_id, project_id)).get(KIND_GIT, []) if project_id else []
        brief["repo_url_prefixes"] = list(repo_url_prefixes(remotes))
        brief["handoff_health"] = stored_health(brief.get("handoff"))
        brief["rendered"] = render_brief(brief)
        police_brief(brief)  # the JSON fields get the same verdicts as the rendered text
        return brief


def _same_session_facts(prev: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when two relay metadata blocks describe the same facts (ignoring close time)."""
    ignore = {"closed_at"}
    return {k: v for k, v in prev.items() if k not in ignore} == {k: v for k, v in new.items() if k not in ignore}


_UNIX_EPOCH_JULIAN = 2440587.5


def _from_julian(jd: float) -> datetime:
    """SQLite ``julianday()`` (UTC) -> aware UTC datetime, millisecond precision."""
    seconds = round((jd - _UNIX_EPOCH_JULIAN) * 86400.0, 3)
    return datetime.fromtimestamp(seconds, tz=UTC)


def _trail_detail(memory: dict[str, Any], relay: dict[str, Any]) -> dict[str, Any]:
    """Expandable detail for one trail entry (structured sections or raw content)."""
    if not relay:
        return {"structured": False, "content": str(memory.get("content") or "")[:4000]}
    return {
        "structured": True,
        "done": list(relay.get("done") or []),
        "not_done": list(relay.get("not_done") or []),
        "failing": list(relay.get("failing") or []),
        "next": relay.get("next"),
        "next_source": relay.get("next_source"),
        "commits": list(relay.get("commits") or []),
        "upstream": relay.get("upstream"),
        "unpushed_commits": relay.get("unpushed_commits"),
        "files_changed_count": relay.get("files_changed_count"),
        "uncommitted_count": relay.get("uncommitted_count"),
        "grounding_status": (relay.get("grounding") or {}).get("status"),
        "agent_verified": bool(relay.get("agent_verified")),
        "end_reason": relay.get("end_reason"),
    }
