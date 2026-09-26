"""Crew tasks: the task state machine, same-crew dependencies, acceptance lock and task-linked claims (WP-6, spec §5.4).

Status machine (§5.4)::

    backlog → ready (deps done) → claimed (owner set, zones claimed) → in_progress ⇄ blocked
    in_progress → review → done | in_progress (rejected)
    claimed|in_progress|blocked ─owner lost/quota/ended/released unfinished─▶ stalled ─adopt/assign─▶ claimed
    stalled ─owner recovers (not adopted)─▶ status_before_stall (stalled report superseded, §10.2)
    any non-done → cancelled ; done → reopen → ready

Guards: ``claimed`` needs every dependency done (a human ``assign`` overrides);
WIP is limited to ``wip_per_session``; ``PATCH status=done`` is refused with 409
``report_required`` (only a report or a human waiver finishes a task);
``started_head`` anchors the report's commit range; dependencies must be tasks of
the same crew and may not form a cycle. Acceptance criteria are validated here
(``match`` is an argv-prefix pattern, never executed; ``deploy`` URLs are https and
fetched only by :mod:`remembra.crew.livecheck`) and lock when the task first reaches
``in_progress``; after that only a human may change them.

Every mutation runs in one ``BEGIN IMMEDIATE`` crew.db transaction
(:meth:`CrewEventLog.transaction`) with its events.

Integration seams (named here so the owning packages can take them over):

* **Claims (WP-5).** Starting a task claims all its zones, all or nothing; finishing
  releases them; leaving unfinished reserves them (the baton). Tasks reach claims
  only through the :class:`TaskClaims` protocol. :class:`SqlTaskClaims` is the
  working implementation shipped with WP-6 (compatibility, overlaps, parent/child
  zones, per-session and per-agent caps, frozen/protected/crew-policy zones, epochs,
  the ``uq_claim_exclusive`` index as last line of defence); WP-5's claim service
  can replace it by implementing the same five methods.
* **Inbox (WP-7).** :func:`open_inbox_item` / :func:`resolve_inbox_items` write
  ``crew_inbox_items`` with the dedupe index and emit ``inbox.*`` events.
* **Sessions and reaper (WP-4).** :meth:`TaskService.stall_session_tasks` and
  :meth:`TaskService.recover_session_tasks` are the task side of lost / quota /
  dirty-end and of recovery (§10.2).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from remembra.crew import schemas
from remembra.crew.events import Actor, CrewEventLog, EventTx
from remembra.crew.settings import load_settings
from remembra.crew.store import dumps, loads, new_id, now_iso, parse_iso

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_STATUSES: Final = ("backlog", "ready", "claimed", "in_progress", "blocked", "review", "stalled")
OWNED_STATUSES: Final = ("claimed", "in_progress", "blocked", "review")
WIP_STATUSES: Final = ("claimed", "in_progress", "blocked")
UNFINISHED_STATUSES: Final = ("claimed", "in_progress", "blocked")
# Statuses that must carry exactly one current report once the task reached in_progress (§5.6).
FINAL_REPORT_STATUSES: Final = ("done", "stalled", "cancelled")
MAX_DEPENDENCIES: Final = 50
MAX_CRITERIA: Final = 20
LIVE_CLAIM_HOLD_STATES: Final = ("active", "offered", "reserved")
PATCHABLE_FIELDS: Final = frozenset(
    {"title", "body", "phase", "priority", "zone_ids", "labels", "reviewer", "acceptance", "status", "position"}
)
HUMAN_REVIEW_ACTIONS: Final = ("approve", "reject")


# ---------------------------------------------------------------------------
# Errors and callers
# ---------------------------------------------------------------------------


class CrewServiceError(Exception):
    """A refused crew action. Routes map it to ``crew_error(status, error, message, **extra)``."""

    def __init__(self, status: int, error: str, message: str, **extra: Any) -> None:
        self.status = status
        self.error = error
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}
        super().__init__(f"{error}: {message}")


def _err(status: int, error: str, message: str, **extra: Any) -> CrewServiceError:
    return CrewServiceError(status, error, message, **extra)


@dataclass(frozen=True)
class Caller:
    """Who performs a task action: a crew session (session token), a human (dashboard JWT) or the server."""

    user_id: str
    human: bool = False
    session: Mapping[str, Any] | None = None
    system: bool = False

    @classmethod
    def for_session(cls, session: Mapping[str, Any]) -> Caller:
        return cls(user_id=str(session["user_id"]), session=dict(session))

    @classmethod
    def for_human(cls, user_id: str) -> Caller:
        """Only for a principal the route checked with ``is_human`` (D27)."""
        return cls(user_id=user_id, human=True)

    @classmethod
    def server(cls) -> Caller:
        return cls(user_id="server", system=True)

    @property
    def session_id(self) -> str | None:
        return str(self.session["id"]) if self.session else None

    @property
    def actor(self) -> Actor:
        if self.session is not None:
            s = self.session
            return Actor.session(
                str(s["id"]),
                callsign=str(s["callsign"]),
                agent_id=str(s["agent_id"]),
                user_id=str(s["user_id"]),
                verified=bool(s.get("agent_verified")),
            )
        if self.human:
            return Actor.human(self.user_id)
        return Actor.system()

    @property
    def label(self) -> str:
        """Who, for server summary templates (callsign, 'human' or 'server'; never free text)."""
        if self.session is not None:
            return str(self.session["callsign"])
        return "human" if self.human else "server"


def session_channel_source(session: Mapping[str, Any] | None) -> str:
    """Evidence source of what a session submits (§5.6): hook and CLI sessions are ``relay-cli``, MCP ``agent-declared``."""
    if session is None:
        return "server-inferred"
    kind = session.get("client_kind")
    adapter = session.get("adapter")
    if kind in ("hook", "cli") and adapter != "mcp":
        return "relay-cli"
    return "agent-declared"


def token_hash(token: str) -> str:
    """How a session token is compared with ``crew_sessions.token_hash`` (sha256 hex; the raw token is never stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


async def fetchall(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = await conn.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    names = [d[0] for d in cursor.description] if cursor.description else []
    await cursor.close()
    return [dict(zip(names, tuple(r), strict=True)) for r in rows]


async def fetchone(conn: Any, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    rows = await fetchall(conn, sql, params)
    return rows[0] if rows else None


async def crew_settings(conn: Any, crew_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT settings FROM crews WHERE id = ?", (crew_id,))
    if row is None:
        raise _err(404, "not_found", "Not found.")
    return load_settings(row["settings"])


async def crew_row(conn: Any, crew_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT * FROM crews WHERE id = ?", (crew_id,))
    if row is None:
        raise _err(404, "not_found", "Not found.")
    return row


async def load_task(conn: Any, crew_id: str, task_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT * FROM crew_tasks WHERE id = ? AND crew_id = ?", (task_id, crew_id))
    if row is None:
        raise _err(404, "not_found", "Not found.")
    return row


async def load_session(conn: Any, crew_id: str, session_id: str) -> dict[str, Any] | None:
    return await fetchone(conn, "SELECT * FROM crew_sessions WHERE id = ? AND crew_id = ?", (session_id, crew_id))


async def session_for_token(conn: Any, crew_id: str, token: str) -> dict[str, Any] | None:
    """The crew session a raw session token belongs to (None for an unknown token or another crew)."""
    if not token:
        return None
    return await fetchone(conn, "SELECT * FROM crew_sessions WHERE token_hash = ? AND crew_id = ?", (token_hash(token), crew_id))


async def task_deps(conn: Any, crew_id: str, task_id: str) -> list[str]:
    rows = await fetchall(
        conn,
        "SELECT depends_on_id FROM crew_task_deps WHERE crew_id = ? AND task_id = ? ORDER BY depends_on_id",
        (crew_id, task_id),
    )
    return [str(r["depends_on_id"]) for r in rows]


def task_ref(task: Mapping[str, Any]) -> str:
    return f"T-{int(task['number'])}"


def public_criteria(acceptance: Any) -> list[dict[str, Any]]:
    """Criteria as the closed ``Criterion`` shape (waiver bookkeeping stripped)."""
    out: list[dict[str, Any]] = []
    for c in acceptance or []:
        if not isinstance(c, dict):
            continue
        item = {k: c.get(k) for k in ("id", "text", "kind", "match", "url", "required")}
        item["required"] = bool(item["required"])
        out.append(item)
    return out


def waivers_of(acceptance: Any) -> dict[str, dict[str, Any]]:
    return {str(c["id"]): dict(c["waiver"]) for c in acceptance or [] if isinstance(c, dict) and c.get("waiver")}


def task_view(task: Mapping[str, Any], depends_on: Sequence[str]) -> dict[str, Any]:
    """The ``TaskView`` carried by events and snapshots (schemas.TASK_VIEW)."""
    return {
        "id": task["id"],
        "number": int(task["number"]),
        "title": str(task["title"])[:200],
        "status": task["status"],
        "status_before_stall": task.get("status_before_stall"),
        "phase": task.get("phase"),
        "priority": int(task["priority"]) if task.get("priority") is not None else 2,
        "zone_ids": list(loads(task.get("zone_ids"), [])),
        "owner_session_id": task.get("owner_session_id"),
        "owner_agent_id": task.get("owner_agent_id"),
        "reviewer": task.get("reviewer"),
        "depends_on": list(depends_on),
        "acceptance": public_criteria(loads(task.get("acceptance"), [])),
        "acceptance_locked": bool(task.get("acceptance_locked")),
        "started_head": task.get("started_head"),
        "current_report_id": task.get("current_report_id"),
        "blocked_reason": task.get("blocked_reason"),
        "version": int(task["version"]),
    }


def task_detail(task: Mapping[str, Any], depends_on: Sequence[str]) -> dict[str, Any]:
    """REST representation: the view plus free text and bookkeeping fields (never injected into agents)."""
    view = task_view(task, depends_on)
    acceptance = loads(task.get("acceptance"), [])
    waivers = waivers_of(acceptance)
    for c in view["acceptance"]:
        if c["id"] in waivers:
            c["waived"] = {k: waivers[c["id"]].get(k) for k in ("by", "reason", "at")}
    view.update(
        {
            "crew_id": task["crew_id"],
            "ref": task_ref(task),
            "body": task.get("body"),
            "labels": list(loads(task.get("labels"), [])),
            "owner_user_id": task.get("owner_user_id"),
            "started_at": task.get("started_at"),
            "done_at": task.get("done_at"),
            "stalled_at": task.get("stalled_at"),
            "created_by": task.get("created_by"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }
    )
    return view


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_acceptance(criteria: Any) -> list[dict[str, Any]]:
    """Validate acceptance criteria (§5.4); returns the normalised list or raises 422.

    ``test`` / ``command`` need a ``match`` in the argv-prefix grammar (D38; nothing
    ever executes it). ``file`` needs a repo-relative path. ``commit`` may give a sha
    prefix. ``deploy`` needs an ``https`` URL (fetched only by the server live check,
    and only for hosts in ``live_check_domains``). ``manual`` needs neither.
    """
    if criteria is None:
        return []
    if not isinstance(criteria, list):
        raise _err(422, "invalid_acceptance", "acceptance must be a list of criteria")
    if len(criteria) > MAX_CRITERIA:
        raise _err(422, "invalid_acceptance", f"at most {MAX_CRITERIA} criteria")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, c in enumerate(criteria):
        item = dict(c) if isinstance(c, Mapping) else c
        if isinstance(item, dict):
            item.setdefault("match", None)
            item.setdefault("url", None)
            item.setdefault("required", True)
        errors = schemas.validate(item, schemas.CRITERION, f"$.acceptance[{idx}]")
        if errors:
            raise _err(422, "invalid_acceptance", "; ".join(errors[:3]))
        assert isinstance(item, dict)
        cid, kind, match, url = item["id"], item["kind"], item.get("match"), item.get("url")
        if cid in seen:
            raise _err(422, "invalid_acceptance", f"criterion id {cid!r} is used twice")
        seen.add(cid)
        where = f"criterion {cid}"
        if kind in ("test", "command"):
            if not match:
                raise _err(422, "invalid_acceptance", f"{where}: kind {kind} needs a match pattern")
            problems = schemas.validate_command_pattern(match)
            if problems:
                raise _err(422, "invalid_acceptance", f"{where}: match {problems[0]}")
        elif kind == "file":
            if not match or not schemas.is_path_rel(match):
                raise _err(422, "invalid_acceptance", f"{where}: kind file needs a repo-relative path in match")
        elif kind == "commit":
            if match is not None and not _is_sha_prefix(match):
                raise _err(422, "invalid_acceptance", f"{where}: commit match must be a sha prefix (7-40 hex)")
        elif kind == "deploy":
            if not url:
                raise _err(422, "invalid_acceptance", f"{where}: kind deploy needs an https url")
            if match is not None:
                raise _err(422, "invalid_acceptance", f"{where}: kind deploy takes a url, not a match")
        if kind != "deploy" and url is not None:
            raise _err(422, "invalid_acceptance", f"{where}: only deploy criteria take a url")
        out.append(
            {"id": cid, "text": item["text"], "kind": kind, "match": match, "url": url, "required": bool(item["required"])}
        )
    return out


def _is_sha_prefix(value: Any) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(schemas.SHA_PATTERN, value.lower()) is not None


def _check_text(value: Any, name: str, max_len: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _err(422, "invalid_task", f"{name} is required")
        return None
    if not isinstance(value, str):
        raise _err(422, "invalid_task", f"{name} must be a string")
    clean = value.strip()
    if required and not clean:
        raise _err(422, "invalid_task", f"{name} must not be empty")
    if len(value) > max_len:
        raise _err(422, "invalid_task", f"{name} is longer than {max_len} characters")
    return value


def _check_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
        raise _err(422, "invalid_task", "priority must be an integer from 0 to 4")
    return value


def _check_labels(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 20 or not all(isinstance(v, str) and 0 < len(v) <= 40 for v in value):
        raise _err(422, "invalid_task", "labels must be a list of at most 20 short strings")
    return list(dict.fromkeys(value))


async def _check_zone_ids(conn: Any, crew_id: str, zone_ids: Any) -> list[str]:
    if not isinstance(zone_ids, list) or len(zone_ids) > 20:
        raise _err(422, "invalid_task", "zone_ids must be a list of at most 20 zone ids")
    clean = list(dict.fromkeys(zone_ids))
    for zid in clean:
        row = (
            await fetchone(conn, "SELECT id, archived_at FROM crew_zones WHERE id = ? AND crew_id = ?", (zid, crew_id))
            if schemas.is_id("zone", zid)
            else None
        )
        if row is None or row["archived_at"]:
            raise _err(422, "cross_crew_reference", "The referenced zone is not part of this crew.")
    return clean


async def _check_dep_ids(conn: Any, crew_id: str, dep_ids: Any, *, self_id: str | None) -> list[str]:
    if not isinstance(dep_ids, list) or len(dep_ids) > MAX_DEPENDENCIES:
        raise _err(422, "invalid_task", f"depends_on must be a list of at most {MAX_DEPENDENCIES} task ids")
    clean = list(dict.fromkeys(dep_ids))
    for dep in clean:
        if dep == self_id:
            raise _err(422, "dependency_cycle", "A task cannot depend on itself.")
        row = (
            await fetchone(conn, "SELECT id FROM crew_tasks WHERE id = ? AND crew_id = ?", (dep, crew_id))
            if schemas.is_id("task", dep)
            else None
        )
        if row is None:
            raise _err(422, "cross_crew_reference", "The referenced task is not part of this crew.")
    return clean


async def _deps_done(conn: Any, crew_id: str, dep_ids: Sequence[str]) -> bool:
    if not dep_ids:
        return True
    marks = ", ".join("?" for _ in dep_ids)
    row = await fetchone(
        conn,
        f"SELECT COUNT(*) AS n FROM crew_tasks WHERE crew_id = ? AND id IN ({marks}) AND status = 'done'",  # noqa: S608
        (crew_id, *dep_ids),
    )
    return row is not None and int(row["n"]) == len(dep_ids)


async def _would_cycle(conn: Any, crew_id: str, task_id: str, new_dep: str) -> bool:
    """True when ``task_id`` → ``new_dep`` closes a cycle (``new_dep`` already reaches ``task_id``)."""
    seen: set[str] = set()
    stack = [new_dep]
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(await task_deps(conn, crew_id, node))
    return False


# ---------------------------------------------------------------------------
# Inbox items (WP-7 seam)
# ---------------------------------------------------------------------------


def inbox_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "audience": row["audience"],
        "recipient": row.get("recipient"),
        "kind": row["kind"],
        "origin": row["origin"],
        "ref_type": row.get("ref_type"),
        "ref_id": row.get("ref_id"),
        "priority": int(row["priority"]),
        "title": str(row["title"])[:200],
        "primary_action": row.get("primary_action"),
        "state": row["state"],
        "claimed_by": row.get("claimed_by"),
        "coalesced_count": int(row["coalesced_count"]),
    }


async def open_inbox_item(
    tx: EventTx,
    crew_id: str,
    *,
    audience: str,
    kind: str,
    title: str,
    dedupe_key: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
    priority: int = 2,
    recipient: str | None = None,
    primary_action: str | None = None,
    actor: Actor | None = None,
) -> tuple[dict[str, Any], bool]:
    """Open (or coalesce into the open) inbox item for ``dedupe_key``. Titles are server templates (ids only)."""
    now = now_iso()
    existing = await fetchone(
        tx.conn,
        "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
        (crew_id, dedupe_key),
    )
    if existing is not None:
        await tx.conn.execute(
            "UPDATE crew_inbox_items SET coalesced_count = coalesced_count + 1, updated_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
        existing["coalesced_count"] = int(existing["coalesced_count"]) + 1
        return existing, False
    item_id = new_id("inbox_item")
    await tx.conn.execute(
        """INSERT INTO crew_inbox_items (id, crew_id, audience, recipient, kind, origin, ref_type, ref_id, priority, title,
               primary_action, state, dedupe_key, coalesced_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'server', ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?)""",
        (item_id, crew_id, audience, recipient, kind, ref_type, ref_id, priority, title, primary_action, dedupe_key, now, now),
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_inbox_items WHERE id = ?", (item_id,))
    assert row is not None
    result = await tx.emit(
        crew_id=crew_id,
        type="inbox.item_created",
        actor=actor or Actor.system(),
        payload={"item": inbox_view(row)},
        summary=f"inbox {kind} opened for {ref_id or crew_id}",
        refs={"inbox_item_id": item_id, **({"task_id": ref_id} if ref_type == "task" else {})},
    )
    await tx.conn.execute("UPDATE crew_inbox_items SET created_seq = ? WHERE id = ?", (result.seq, item_id))
    return row, True


async def resolve_inbox_items(
    tx: EventTx,
    crew_id: str,
    dedupe_keys: Iterable[str],
    *,
    resolved_by: str,
    actor: Actor | None = None,
) -> list[str]:
    """Resolve every live inbox item with one of ``dedupe_keys``; returns their ids."""
    keys = list(dict.fromkeys(dedupe_keys))
    if not keys:
        return []
    marks = ", ".join("?" for _ in keys)
    rows = await fetchall(
        tx.conn,
        f"SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key IN ({marks})"  # noqa: S608
        " AND state IN ('open','seen','claimed')",
        (crew_id, *keys),
    )
    now = now_iso()
    out: list[str] = []
    for row in rows:
        await tx.conn.execute(
            "UPDATE crew_inbox_items SET state = 'resolved', resolved_by = ?, updated_at = ? WHERE id = ?",
            (resolved_by, now, row["id"]),
        )
        row = {**row, "state": "resolved"}
        result = await tx.emit(
            crew_id=crew_id,
            type="inbox.item_resolved",
            actor=actor or Actor.system(),
            payload={"item": inbox_view(row)},
            summary=f"inbox {row['kind']} resolved for {row.get('ref_id') or crew_id}",
            refs={"inbox_item_id": row["id"], **({"task_id": row["ref_id"]} if row.get("ref_type") == "task" else {})},
        )
        await tx.conn.execute("UPDATE crew_inbox_items SET resolved_seq = ? WHERE id = ?", (result.seq, row["id"]))
        out.append(str(row["id"]))
    return out


class BatonRestoreError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def record_baton_restore(
    events: CrewEventLog,
    crew_id: str,
    session: Mapping[str, Any],
    baton_id: str,
    *,
    restored: bool,
    status: str,
    files: int,
) -> dict[str, Any]:
    """crewd's outcome of restoring a baton ref into the adopter's checkout (D30, §13.3 step 7).

    Only the session the baton passed to may report it, once: sets ``crew_batons.restored`` and
    emits ``baton.restored``. A failed restore (dirty tree, missing ref, git error) is a Needs-you
    safety item on the task, so the owner never takes a failed restore for a successful one.
    A repeated report returns the recorded outcome unchanged (crewd may retry after a timeout).
    """
    if status not in schemas.BATON_RESTORE_STATUSES:
        raise BatonRestoreError(422, "invalid_status", f"status must be one of {', '.join(schemas.BATON_RESTORE_STATUSES)}")
    if bool(restored) != (status == "restored"):
        raise BatonRestoreError(422, "invalid_status", "restored is true exactly when status is 'restored'")
    async with events.transaction() as tx:
        row = await fetchone(tx.conn, "SELECT * FROM crew_batons WHERE id = ? AND crew_id = ?", (baton_id, crew_id))
        if row is None or row["to_session"] != session["id"]:
            raise BatonRestoreError(404, "not_found", "Not found.")
        before = row.get("restored")
        if before is not None and (bool(before) or not restored):
            # recorded already; only a successful retry (`adopt --restore-only`) replaces a failed restore
            return {"baton_id": baton_id, "restored": bool(before), "duplicate": True, "seq": None}
        await tx.conn.execute("UPDATE crew_batons SET restored = ? WHERE id = ?", (1 if restored else 0, baton_id))
        task = await fetchone(tx.conn, "SELECT * FROM crew_tasks WHERE id = ?", (row["task_id"],)) if row.get("task_id") else None
        label = task_ref(task) if task else "the baton"
        actor = Actor.session(
            str(session["id"]),
            callsign=str(session["callsign"]),
            agent_id=str(session["agent_id"]),
            user_id=str(session["user_id"]),
            verified=bool(session.get("agent_verified")),
        )
        result = await tx.emit(
            crew_id=crew_id,
            type="baton.restored",
            actor=actor,
            payload={
                "baton_id": baton_id,
                "task_id": row.get("task_id"),
                "to_session": row["to_session"],
                "baton_ref": row.get("baton_ref"),
                "restored": bool(restored),
                "status": status,
                "files": max(0, int(files)),
            },
            summary=(
                f"{session.get('callsign')} restored the saved work of {label} ({int(files)} files)"
                if restored
                else f"{session.get('callsign')} could not restore the saved work of {label} ({status})"
            ),
            refs={"task_id": row.get("task_id"), "session_id": row["to_session"]},
        )
        if restored and before is not None:
            await resolve_inbox_items(tx, crew_id, [f"baton_restore:{baton_id}"], resolved_by=str(session["id"]))
        if not restored:
            await open_inbox_item(
                tx,
                crew_id,
                audience="project",
                kind="baton_restore_failed",
                title=f"{session.get('callsign')} could not restore the saved work of {label} ({status})",
                dedupe_key=f"baton_restore:{baton_id}",
                ref_type="task" if row.get("task_id") else "session",
                ref_id=str(row.get("task_id") or row["to_session"]),
                priority=1,
                primary_action="review",
            )
    return {"baton_id": baton_id, "restored": bool(restored), "duplicate": False, "seq": result.seq}


def baton_inbox_keys(task_id: str) -> list[str]:
    """The dedupe keys of a task baton's items: this module's ``baton:`` item and the Needs-you
    ``baton_available`` / crew ``baton_reserved`` items the session stall path raises (sessions.py)."""
    return [f"baton:{task_id}", f"baton_available:{task_id}", f"baton_reserved:{task_id}"]


def task_inbox_keys(task_id: str) -> list[str]:
    """Every dedupe key a task's inbox items use (resolved together when the task finishes)."""
    return [f"review_report:{task_id}", f"task_ready:{task_id}", f"task_blocked:{task_id}", *baton_inbox_keys(task_id)]


# ---------------------------------------------------------------------------
# Task-linked claims (WP-5 seam)
# ---------------------------------------------------------------------------


def claim_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "zone_id": row.get("zone_id"),
        "path_glob": row.get("path_glob"),
        "resource": row.get("resource"),
        "mode": row["mode"],
        "holder_kind": row["holder_kind"],
        "holder_session_id": row.get("holder_session_id"),
        "holder_agent_id": row.get("holder_agent_id"),
        "holder_user_id": row.get("holder_user_id"),
        "task_id": row.get("task_id"),
        "state": row["state"],
        "source": row["source"],
        "epoch": int(row["epoch"]),
        "unconfirmed": bool(row.get("unconfirmed")),
        "fenced": False,
        "lease_expires_at": row.get("lease_expires_at"),
        "reserve_reason": row.get("reserve_reason"),
        "reserved_for": row.get("reserved_for"),
        "offered_to": row.get("offered_to"),
        "queue_pos": row.get("queue_pos"),
        "baton_ref": row.get("baton_ref"),
        "granted_at": row.get("granted_at"),
        "version": int(row["version"]),
    }


class TaskClaims(Protocol):
    """What the task machine needs from the claim service (§5.1 "Task link")."""

    async def claim(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        settings: Mapping[str, Any],
        task: Mapping[str, Any],
        session: Mapping[str, Any],
        actor: Actor | None = None,
    ) -> list[dict[str, Any]]:
        """Claim every zone of ``task`` for ``session``, all or nothing; raises 409/423 with blockers."""
        ...

    async def release(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        task_id: str,
        holder_session_id: str | None,
        baton: bool,
        reserve_reason: str = "baton",
        baton_ref: str | None = None,
        actor: Actor,
    ) -> list[str]:
        """Release (or, with ``baton``, reserve for the holder) the task's live claims; returns claim ids."""
        ...

    async def adopt(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        settings: Mapping[str, Any],
        task_id: str,
        to_session: Mapping[str, Any],
        from_session_id: str | None,
        cross_checkout: bool,
        source: str,
        actor: Actor,
    ) -> list[dict[str, Any]]:
        """Move the task's reserved (or live) claims to ``to_session`` with epoch + 1."""
        ...

    async def retake(self, tx: EventTx, *, crew_id: str, settings: Mapping[str, Any], task_id: str, session_id: str) -> list[str]:
        """The holder re-takes its own reserved claims of the task (recovery, epoch + 1)."""
        ...


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso(str(value))
    except ValueError:
        return None


class SqlTaskClaims:
    """Task-linked claims written straight to ``crew_claims`` (see the module docstring)."""

    async def _zone_family(self, conn: Any, crew_id: str, zone_id: str) -> set[str]:
        """The zone, its ancestors, its descendants and every zone it overlaps."""
        zones = await fetchall(conn, "SELECT id, parent_id FROM crew_zones WHERE crew_id = ? AND archived_at IS NULL", (crew_id,))
        parent = {z["id"]: z["parent_id"] for z in zones}
        family = {zone_id}
        node = parent.get(zone_id)
        while node and node not in family:
            family.add(node)
            node = parent.get(node)
        children: dict[str, list[str]] = {}
        for zid, pid in parent.items():
            if pid:
                children.setdefault(pid, []).append(zid)
        stack = list(children.get(zone_id, []))
        while stack:
            zid = stack.pop()
            if zid not in family:
                family.add(zid)
                stack.extend(children.get(zid, []))
        for row in await fetchall(
            conn,
            "SELECT zone_a, zone_b FROM crew_zone_overlaps WHERE crew_id = ? AND (zone_a = ? OR zone_b = ?)",
            (crew_id, zone_id, zone_id),
        ):
            family.add(row["zone_b"] if row["zone_a"] == zone_id else row["zone_a"])
        return family

    async def claim(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        settings: Mapping[str, Any],
        task: Mapping[str, Any],
        session: Mapping[str, Any],
        actor: Actor | None = None,
    ) -> list[dict[str, Any]]:
        conn = tx.conn
        zone_ids = list(loads(task.get("zone_ids"), []))
        mode = str(task.get("claim_mode") or "exclusive")
        if mode not in schemas.CLAIM_MODES:
            mode = "exclusive"
        sid = str(session["id"])
        now_dt = datetime.now(UTC)
        now = now_iso(now_dt)
        if settings.get("require_verified_agents_for_claims") and not session.get("agent_verified"):
            raise _err(403, "unverified_agent", "This crew requires key-verified agents to claim zones.")
        zones: list[dict[str, Any]] = []
        for zid in zone_ids:
            zone = await fetchone(conn, "SELECT * FROM crew_zones WHERE id = ? AND crew_id = ?", (zid, crew_id))
            if zone is None or zone["archived_at"]:
                raise _err(422, "cross_crew_reference", "The referenced zone is not part of this crew.")
            if zone["builtin"] or zone["slug"] == "crew-policy":
                raise _err(423, "crew_policy", f"Zone {zone['slug']} is crew policy and cannot be claimed by an agent.")
            frozen_until = _parse_ts(zone.get("frozen_until"))
            if zone.get("frozen_by") and (frozen_until is None or frozen_until > now_dt):
                raise _err(423, "frozen", f"Zone {zone['slug']} is frozen by a human.")
            if zone.get("protected"):
                raise _err(423, "protected", f"Zone {zone['slug']} is protected; a human must grant it.")
            reserve_for = zone.get("reserve_for")
            if reserve_for and (reserve_for != session.get("agent_id") or not session.get("agent_verified")):
                raise _err(423, "reserved_for_agent", f"Zone {zone['slug']} is reserved for a key-verified {reserve_for}.")
            zones.append(zone)

        blockers: list[dict[str, Any]] = []
        reuse: dict[str, dict[str, Any]] = {}
        for zone in zones:
            family = await self._zone_family(conn, crew_id, zone["id"])
            marks = ", ".join("?" for _ in family)
            live = await fetchall(
                conn,
                f"SELECT c.*, s.callsign AS holder_callsign FROM crew_claims c"  # noqa: S608
                f" LEFT JOIN crew_sessions s ON s.id = c.holder_session_id"
                f" WHERE c.crew_id = ? AND c.zone_id IN ({marks}) AND c.state IN ('active','offered','reserved')",
                (crew_id, *family),
            )
            for c in live:
                own = c["holder_kind"] == "session" and c["holder_session_id"] == sid
                if own and c["zone_id"] == zone["id"] and c["state"] in ("active", "reserved"):
                    if c["state"] == "active" or c.get("reserved_for") == sid:
                        reuse[zone["id"]] = c
                    continue
                if own:
                    continue
                if c["state"] == "reserved":
                    reason = "reserved"
                elif mode == "watch" or c["mode"] == "watch" or mode == "shared" and c["mode"] == "shared":
                    continue
                else:
                    reason = f"{c['mode']}_held"
                blockers.append(
                    {
                        "claim_id": c["id"],
                        "zone_id": c["zone_id"],
                        "holder_session_id": c.get("holder_session_id"),
                        "holder_callsign": c.get("holder_callsign"),
                        "task_id": c.get("task_id"),
                        "reason": reason,
                    }
                )
        if blockers:
            first = blockers[0]
            who = first.get("holder_callsign") or "a human"
            raise _err(
                409,
                "conflict",
                f"Zone {first['zone_id']} is held by {who}; the task's zones are claimed all or nothing.",
                blockers=blockers[:10],
            )

        new_exclusive = sum(1 for z in zones if z["id"] not in reuse) if mode == "exclusive" else 0
        if new_exclusive:
            per_session = await fetchone(
                conn,
                "SELECT COUNT(*) AS n FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND mode = 'exclusive'"
                " AND state IN ('active','offered','reserved')",
                (crew_id, sid),
            )
            per_agent = await fetchone(
                conn,
                "SELECT COUNT(*) AS n FROM crew_claims WHERE crew_id = ? AND holder_agent_id = ? AND mode = 'exclusive'"
                " AND state IN ('active','offered','reserved')",
                (crew_id, session["agent_id"]),
            )
            cap_s = int(settings["max_exclusive_claims_per_session"])
            cap_a = int(settings["max_exclusive_claims_per_agent"])
            if per_session and int(per_session["n"]) + new_exclusive > cap_s:
                raise _err(409, "claim_cap", f"At most {cap_s} live exclusive claims per session.")
            if per_agent and int(per_agent["n"]) + new_exclusive > cap_a:
                raise _err(409, "claim_cap", f"At most {cap_a} live exclusive claims per agent.")

        lease = now_iso(now_dt + timedelta(seconds=int(settings["lease_ttl_s"])))
        out: list[dict[str, Any]] = []
        for zone in zones:
            existing = reuse.get(zone["id"])
            if existing is not None:
                bump = 1 if existing["state"] == "reserved" else 0
                await conn.execute(
                    "UPDATE crew_claims SET task_id = ?, state = 'active', epoch = epoch + ?, reserve_reason = NULL,"
                    " reserved_for = NULL, reserve_expires_at = NULL, lease_expires_at = ?, version = version + 1,"
                    " updated_at = ? WHERE id = ?",
                    (task["id"], bump, lease, now, existing["id"]),
                )
                claim_id = str(existing["id"])
            else:
                claim_id = new_id("claim")
                try:
                    await conn.execute(
                        """INSERT INTO crew_claims (id, crew_id, zone_id, mode, holder_kind, holder_session_id, holder_user_id,
                               holder_agent_id, task_id, state, source, epoch, lease_expires_at, granted_at, created_at,
                               updated_at)
                           VALUES (?, ?, ?, ?, 'session', ?, ?, ?, ?, 'active', 'task', 1, ?, ?, ?, ?)""",
                        (
                            claim_id,
                            crew_id,
                            zone["id"],
                            mode,
                            sid,
                            session["user_id"],
                            session["agent_id"],
                            task["id"],
                            lease,
                            now,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise _err(409, "conflict", f"Zone {zone['slug']} was claimed by another session a moment ago.")
            row = await fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (claim_id,))
            assert row is not None
            view = claim_view(row)
            await tx.emit(
                crew_id=crew_id,
                type="claim.granted",
                actor=actor or Caller.for_session(session).actor,
                payload={"claim": view},
                summary=f"{session['callsign']} claimed zone {zone['slug']} ({mode}) for T-{int(task['number'])}",
                refs={"zone_id": zone["id"], "claim_id": claim_id, "task_id": task["id"], "session_id": sid},
            )
            out.append(view)
        return out

    async def release(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        task_id: str,
        holder_session_id: str | None,
        baton: bool,
        reserve_reason: str = "baton",
        baton_ref: str | None = None,
        actor: Actor,
    ) -> list[str]:
        conn = tx.conn
        sql = "SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND holder_kind = 'session'"
        params: list[Any] = [crew_id, task_id]
        if holder_session_id is not None:
            sql += " AND holder_session_id = ?"
            params.append(holder_session_id)
        sql += " AND state IN ('active','offered','reserved')" if not baton else " AND state IN ('active','offered')"
        now = now_iso()
        out: list[str] = []
        freed = False
        for c in await fetchall(conn, sql, params):
            if baton:
                # An explicit release hands the baton on (§5.1 release(baton) → reserved → adopt): it is reserved
                # for the next authorised pickup, not for the releaser, or no live crew could ever adopt it.
                # A stall (lost, quota, ...) keeps it for the holder, which re-takes it on recovery (§10.2).
                await conn.execute(
                    "UPDATE crew_claims SET state = 'reserved', reserve_reason = ?,"
                    " reserved_for = CASE WHEN ? = 'baton' THEN NULL ELSE holder_session_id END,"
                    " reserve_expires_at = NULL, offered_to = NULL, offer_expires_at = NULL, baton_ref = COALESCE(?, baton_ref),"
                    " version = version + 1, updated_at = ? WHERE id = ?",
                    (reserve_reason, reserve_reason, baton_ref, now, c["id"]),
                )
            else:
                await conn.execute(
                    "UPDATE crew_claims SET state = 'released', ended_at = ?, end_reason = 'task', version = version + 1,"
                    " updated_at = ? WHERE id = ?",
                    (now, now, c["id"]),
                )
                freed = True
            row = await fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (c["id"],))
            assert row is not None
            refs = {
                "zone_id": row.get("zone_id"),
                "claim_id": row["id"],
                "task_id": task_id,
                "session_id": row.get("holder_session_id"),
            }
            if baton:
                await tx.emit(
                    crew_id=crew_id,
                    type="claim.reserved",
                    actor=actor,
                    payload={"claim": claim_view(row), "reason": reserve_reason},
                    summary=f"claim {row['id']} reserved ({reserve_reason})",
                    refs=refs,
                )
            else:
                await tx.emit(
                    crew_id=crew_id,
                    type="claim.released",
                    actor=actor,
                    payload={"claim": claim_view(row), "baton": False},
                    summary=f"claim {row['id']} released",
                    refs=refs,
                )
            out.append(str(row["id"]))
        if freed:
            # the task released its zones: whoever queued behind them gets them now (§5.1, FIFO)
            from remembra.crew.claims import promote_queue_in

            await promote_queue_in(tx, crew_id)
        return out

    async def adopt(
        self,
        tx: EventTx,
        *,
        crew_id: str,
        settings: Mapping[str, Any],
        task_id: str,
        to_session: Mapping[str, Any],
        from_session_id: str | None,
        cross_checkout: bool,
        source: str,
        actor: Actor,
    ) -> list[dict[str, Any]]:
        conn = tx.conn
        now_dt = datetime.now(UTC)
        now = now_iso(now_dt)
        lease = now_iso(now_dt + timedelta(seconds=int(settings["lease_ttl_s"])))
        rows = await fetchall(
            conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND holder_kind = 'session'"
            " AND state IN ('active','offered','reserved')",
            (crew_id, task_id),
        )
        out: list[dict[str, Any]] = []
        for c in rows:
            if c["holder_session_id"] == to_session["id"] and c["state"] == "active":
                out.append(claim_view(c))
                continue
            await conn.execute(
                "UPDATE crew_claims SET holder_session_id = ?, holder_user_id = ?, holder_agent_id = ?, state = 'active',"
                " epoch = epoch + 1, source = ?, reserve_reason = NULL, reserved_for = NULL, reserve_expires_at = NULL,"
                " offered_to = NULL, offer_expires_at = NULL, lease_expires_at = ?, granted_at = ?, version = version + 1,"
                " updated_at = ? WHERE id = ?",
                (to_session["id"], to_session["user_id"], to_session["agent_id"], source, lease, now, now, c["id"]),
            )
            row = await fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (c["id"],))
            assert row is not None
            view = claim_view(row)
            refs = {"zone_id": row.get("zone_id"), "claim_id": row["id"], "task_id": task_id, "session_id": to_session["id"]}
            if source == "dashboard":
                await tx.emit(
                    crew_id=crew_id,
                    type="claim.transferred",
                    actor=actor,
                    payload={"claim": view, "from_session": c.get("holder_session_id"), "reason": "task assigned"},
                    summary=f"claim {row['id']} transferred to {to_session['callsign']}",
                    refs=refs,
                )
            else:
                await tx.emit(
                    crew_id=crew_id,
                    type="claim.adopted",
                    actor=actor,
                    payload={"claim": view, "cross_checkout": cross_checkout, "from_session": from_session_id},
                    summary=f"{to_session['callsign']} adopted claim {row['id']}",
                    refs=refs,
                )
            out.append(view)
        return out

    async def retake(self, tx: EventTx, *, crew_id: str, settings: Mapping[str, Any], task_id: str, session_id: str) -> list[str]:
        conn = tx.conn
        now_dt = datetime.now(UTC)
        now = now_iso(now_dt)
        lease = now_iso(now_dt + timedelta(seconds=int(settings["lease_ttl_s"])))
        rows = await fetchall(
            conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND holder_session_id = ? AND state = 'reserved'"
            " AND (reserved_for IS NULL OR reserved_for = ?)",
            (crew_id, task_id, session_id, session_id),
        )
        out: list[str] = []
        for c in rows:
            await conn.execute(
                "UPDATE crew_claims SET state = 'active', epoch = epoch + 1, reserve_reason = NULL, reserved_for = NULL,"
                " reserve_expires_at = NULL, lease_expires_at = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (lease, now, c["id"]),
            )
            out.append(str(c["id"]))
        return out


# ---------------------------------------------------------------------------
# Reports: the low-level row writer shared with crew/reports.py
# ---------------------------------------------------------------------------


def report_view(row: Mapping[str, Any]) -> dict[str, Any]:
    criteria = loads(row.get("criteria"), []) or []
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "session_id": row.get("session_id"),
        "kind": row["kind"],
        "verdict": row.get("verdict"),
        "review_state": row.get("review_state"),
        "is_current": bool(row["is_current"]),
        "superseded_reason": row.get("superseded_reason"),
        "facts_source": row["facts_source"],
        "criteria": [{"id": c["id"], "status": c["status"], "source": c.get("source")} for c in criteria if isinstance(c, dict)][
            :20
        ],
        "baton_ref": row.get("baton_ref"),
        "handoff_id": row.get("handoff_id"),
    }


async def supersede_current_report(tx: EventTx, crew_id: str, task: Mapping[str, Any], reason: str, actor: Actor) -> str | None:
    """Mark the task's current report not current (same transaction as its replacement, §5.6)."""
    row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task["id"],))
    if row is None:
        return None
    await tx.conn.execute("UPDATE crew_reports SET is_current = 0, superseded_reason = ? WHERE id = ?", (reason[:64], row["id"]))
    row = {**row, "is_current": 0, "superseded_reason": reason[:64]}
    await tx.emit(
        crew_id=crew_id,
        type="report.superseded",
        actor=actor,
        payload={"report": report_view(row)},
        summary=f"report {row['id']} for {task_ref(task)} superseded ({reason[:32]})",
        refs={"task_id": task["id"], "report_id": row["id"]},
    )
    return str(row["id"])


async def insert_report(
    tx: EventTx,
    crew_id: str,
    task: Mapping[str, Any],
    *,
    kind: str,
    session_id: str | None,
    facts_source: str,
    actor: Actor,
    verdict: str | None = None,
    review_state: str | None = None,
    criteria: list[dict[str, Any]] | None = None,
    commits: list[Any] | None = None,
    files: list[Any] | None = None,
    out_of_zone_files: list[Any] | None = None,
    tests: list[Any] | None = None,
    deploy: Mapping[str, Any] | None = None,
    sections: Mapping[str, Any] | None = None,
    summary: str | None = None,
    grounding: Mapping[str, Any] | None = None,
    facts_hash: str | None = None,
    handoff_id: str | None = None,
    baton_ref: str | None = None,
    supersede_reason: str = "replaced",
    event_type: str = "report.submitted",
) -> dict[str, Any]:
    """Insert a new **current** report, superseding the previous one in the same transaction (§5.6)."""
    await supersede_current_report(tx, crew_id, task, supersede_reason, actor)
    report_id = new_id("report")
    now = now_iso()
    await tx.conn.execute(
        """INSERT INTO crew_reports (id, crew_id, task_id, session_id, kind, verdict, criteria, commits, files,
               out_of_zone_files, tests, deploy, sections, summary, grounding, facts_source, facts_hash, review_state,
               is_current, handoff_id, baton_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (
            report_id,
            crew_id,
            task["id"],
            session_id,
            kind,
            verdict,
            dumps(criteria or []),
            dumps(commits or []),
            dumps(files or []),
            dumps(out_of_zone_files or []),
            dumps(tests or []),
            dumps(dict(deploy or {})),
            dumps(dict(sections or {})),
            summary,
            dumps(dict(grounding)) if grounding is not None else None,
            facts_source,
            facts_hash or hashlib.sha256(f"{report_id}".encode()).hexdigest(),
            review_state,
            handoff_id,
            baton_ref,
            now,
        ),
    )
    await tx.conn.execute(
        "UPDATE crew_tasks SET current_report_id = ?, updated_at = ? WHERE id = ?", (report_id, now, task["id"])
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (report_id,))
    assert row is not None
    await tx.emit(
        crew_id=crew_id,
        type=event_type,
        actor=actor,
        payload={"report": report_view(row)},
        summary=f"{kind} report {report_id} for {task_ref(task)}" + (f" ({verdict})" if verdict else ""),
        refs={"task_id": task["id"], "report_id": report_id, "session_id": session_id},
    )
    return row


async def last_checkpoint_facts(conn: Any, crew_id: str, session_id: str | None) -> tuple[dict[str, Any], str | None]:
    """The facts and id of a session's latest checkpoint (for server-synthesized reports)."""
    if not session_id:
        return {}, None
    row = await fetchone(
        conn,
        "SELECT id, facts FROM crew_checkpoints WHERE crew_id = ? AND session_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (crew_id, session_id),
    )
    if row is None:
        return {}, None
    facts = loads(row["facts"], {}) or {}
    return (facts if isinstance(facts, dict) else {}), str(row["id"])


async def synthesize_report(
    tx: EventTx,
    crew_id: str,
    task: Mapping[str, Any],
    *,
    kind: str,
    reason: str,
    actor: Actor,
    session_id: str | None = None,
    baton_ref: str | None = None,
    handoff_id: str | None = None,
    supersede_reason: str = "replaced",
) -> dict[str, Any]:
    """A ``server-inferred`` report from the owner's last checkpoint (stall, lost, release, cancel)."""
    facts, checkpoint_id = await last_checkpoint_facts(tx.conn, crew_id, session_id)
    commits = [c.get("sha") if isinstance(c, dict) else c for c in facts.get("commits") or []][:200]
    dirty = facts.get("uncommitted_files") or facts.get("dirty") or []
    tests = facts.get("tests") or []
    failing = [t for t in tests if isinstance(t, dict) and (t.get("passed") is False or int(t.get("failed") or 0) > 0)]
    sections = {
        "done": [f"{len(commits)} commit(s) recorded"] if commits else [],
        "not_done": [f"{len(dirty)} uncommitted file(s)"] if dirty else [],
        "failing": [f"{len(failing)} failing test run(s)"] if failing else [],
        "next": [],
        "follow_ups": [],
    }
    criteria = [
        {"id": c["id"], "status": "waived" if c.get("waiver") else "unknown", "source": None}
        for c in loads(task.get("acceptance"), []) or []
        if isinstance(c, dict) and c.get("required", True)
    ]
    return await insert_report(
        tx,
        crew_id,
        task,
        kind=kind,
        session_id=session_id,
        facts_source="server-inferred",
        actor=actor,
        verdict="partial" if kind in ("partial", "stalled") else None,
        criteria=criteria,
        commits=commits,
        files=[str(f) for f in dirty][:200],
        tests=tests[:50],
        deploy={"unpushed": facts.get("unpushed_commits"), "checkpoint_id": checkpoint_id},
        sections=sections,
        summary=None,
        grounding={"status": "none", "issues": [], "checked": [], "reason": reason[:64]},
        facts_hash=hashlib.sha256(f"{task['id']}:{kind}:{reason}:{now_iso()}:{new_id('report')}".encode()).hexdigest(),
        handoff_id=handoff_id,
        baton_ref=baton_ref,
        supersede_reason=supersede_reason,
    )


# ---------------------------------------------------------------------------
# The task service
# ---------------------------------------------------------------------------

CheckpointHook = Callable[[EventTx, str, Mapping[str, Any], Mapping[str, Any], str, str], Awaitable[Any]]


@dataclass
class TaskResult:
    task: dict[str, Any]
    seq: int | None
    extra: dict[str, Any] = field(default_factory=dict)


class TaskService:
    """The task state machine over ``crew.db`` (see the module docstring)."""

    def __init__(
        self,
        events: CrewEventLog,
        *,
        claims: TaskClaims | None = None,
        on_transition: CheckpointHook | None = None,
    ) -> None:
        self.events = events
        self.claims: TaskClaims = claims or SqlTaskClaims()
        # Called with (tx, crew_id, task, session, from_status, to_status) after an owner transition;
        # crew/checkpoints.py records the ``task`` trigger checkpoint through it (§5.5).
        self.on_transition = on_transition

    # -- helpers ---------------------------------------------------------------

    async def _emit_task(
        self,
        tx: EventTx,
        crew_id: str,
        task_id: str,
        event_type: str,
        caller: Caller,
        summary: str,
        **payload: Any,
    ) -> tuple[dict[str, Any], int]:
        task = await load_task(tx.conn, crew_id, task_id)
        deps = await task_deps(tx.conn, crew_id, task_id)
        result = await tx.emit(
            crew_id=crew_id,
            type=event_type,
            actor=caller.actor,
            payload={"task": task_view(task, deps), **payload},
            summary=summary,
            refs={"task_id": task_id, "session_id": task.get("owner_session_id") or caller.session_id},
        )
        return task_detail(task, deps), result.seq

    async def _set_status(
        self,
        tx: EventTx,
        crew_id: str,
        task: Mapping[str, Any],
        to: str,
        caller: Caller,
        *,
        sets: Mapping[str, Any] | None = None,
        event_type: str = "task.status_changed",
        extra_payload: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        frm = str(task["status"])
        columns = {"status": to, "updated_at": now_iso(), **(sets or {})}
        assignments = ", ".join(f"{k} = ?" for k in columns)
        cursor = await tx.conn.execute(
            f"UPDATE crew_tasks SET {assignments}, version = version + 1"  # noqa: S608
            " WHERE id = ? AND crew_id = ? AND version = ?",
            (*columns.values(), task["id"], crew_id, task["version"]),
        )
        if (cursor.rowcount or 0) != 1:
            raise _err(409, "conflict", "The task changed while this action ran; reload and retry.")
        payload: dict[str, Any] = dict(extra_payload or {})
        if event_type == "task.status_changed":
            payload.update({"from": frm, "to": to})
        return await self._emit_task(
            tx, crew_id, str(task["id"]), event_type, caller, f"{task_ref(task)} {frm} -> {to} by {caller.label}", **payload
        )

    async def _transition_hook(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], session: Mapping[str, Any] | None, frm: str, to: str
    ) -> None:
        if self.on_transition is not None and session is not None:
            await self.on_transition(tx, crew_id, task, session, frm, to)

    async def _promote_dependents(self, tx: EventTx, crew_id: str, done_task_id: str, caller: Caller) -> list[str]:
        """Backlog tasks whose dependencies are now all done become ``ready`` (and appear in the crew inbox)."""
        rows = await fetchall(
            tx.conn,
            "SELECT t.* FROM crew_tasks t JOIN crew_task_deps d ON d.task_id = t.id AND d.crew_id = t.crew_id"
            " WHERE d.crew_id = ? AND d.depends_on_id = ? AND t.status = 'backlog'",
            (crew_id, done_task_id),
        )
        out: list[str] = []
        for t in rows:
            if await _deps_done(tx.conn, crew_id, await task_deps(tx.conn, crew_id, t["id"])):
                await self._set_status(tx, crew_id, t, "ready", Caller.server())
                await open_inbox_item(
                    tx,
                    crew_id,
                    audience="crew",
                    kind="task_ready",
                    title=f"{task_ref(t)} is ready to pick up",
                    dedupe_key=f"task_ready:{t['id']}",
                    ref_type="task",
                    ref_id=t["id"],
                )
                out.append(str(t["id"]))
        return out

    def _require_owner(self, task: Mapping[str, Any], caller: Caller) -> None:
        if caller.human or caller.system:
            return
        if caller.session_id is None or task.get("owner_session_id") != caller.session_id:
            raise _err(403, "not_task_owner", f"Only the session that owns {task_ref(task)} can do this.")

    def _require_session(self, caller: Caller) -> Mapping[str, Any]:
        if caller.session is None:
            raise _err(403, "session_required", "This action is performed by a crew session (send its session token).")
        if caller.session.get("state") == "ended":
            raise _err(409, "session_ended", "This crew session has ended.")
        return caller.session

    # -- reads -------------------------------------------------------------------

    async def get(self, crew_id: str, task_id: str) -> dict[str, Any]:
        conn = self.events.db.conn
        task = await load_task(conn, crew_id, task_id)
        return task_detail(task, await task_deps(conn, crew_id, task_id))

    async def list_tasks(self, crew_id: str, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        conn = self.events.db.conn
        if status is not None and status not in schemas.TASK_STATUSES:
            raise _err(422, "invalid_filter", f"status must be one of {', '.join(schemas.TASK_STATUSES)}")
        sql = "SELECT * FROM crew_tasks WHERE crew_id = ?"
        params: list[Any] = [crew_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY number LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = await fetchall(conn, sql, params)
        deps = await fetchall(conn, "SELECT task_id, depends_on_id FROM crew_task_deps WHERE crew_id = ?", (crew_id,))
        by_task: dict[str, list[str]] = {}
        for d in deps:
            by_task.setdefault(str(d["task_id"]), []).append(str(d["depends_on_id"]))
        return [task_detail(r, sorted(by_task.get(str(r["id"]), []))) for r in rows]

    # -- create / patch ----------------------------------------------------------

    async def create(self, crew_id: str, caller: Caller, body: Mapping[str, Any]) -> TaskResult:
        errors = schemas.validate({k: body.get(k) for k in body}, schemas.REQUEST_SHAPES["TaskCreate"], "$")
        if errors:
            raise _err(422, "invalid_task", "; ".join(errors[:3]))
        title = _check_text(body.get("title"), "title", 200, required=True)
        assert title is not None
        acceptance = validate_acceptance(body.get("acceptance") or [])
        priority = _check_priority(body.get("priority", 2))
        async with self.events.transaction() as tx:
            zone_ids = await _check_zone_ids(tx.conn, crew_id, body.get("zone_ids") or [])
            dep_ids = await _check_dep_ids(tx.conn, crew_id, body.get("depends_on") or [], self_id=None)
            status = "ready" if await _deps_done(tx.conn, crew_id, dep_ids) else "backlog"
            number = await self._next_number(tx, crew_id)
            task_id = new_id("task")
            now = now_iso()
            await tx.conn.execute(
                """INSERT INTO crew_tasks (id, crew_id, number, title, body, status, phase, priority, zone_ids, reviewer,
                       acceptance, created_by, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    task_id,
                    crew_id,
                    number,
                    title,
                    body.get("body"),
                    status,
                    body.get("phase"),
                    priority,
                    dumps(zone_ids),
                    body.get("reviewer"),
                    dumps(acceptance),
                    caller.session_id or caller.user_id,
                    now,
                    now,
                ),
            )
            for dep in dep_ids:
                await tx.conn.execute(
                    "INSERT INTO crew_task_deps (crew_id, task_id, depends_on_id) VALUES (?, ?, ?)", (crew_id, task_id, dep)
                )
            task, seq = await self._emit_task(tx, crew_id, task_id, "task.created", caller, f"{caller.label} created T-{number}")
            if status == "ready":
                await open_inbox_item(
                    tx,
                    crew_id,
                    audience="crew",
                    kind="task_ready",
                    title=f"T-{number} is ready to pick up",
                    dedupe_key=f"task_ready:{task_id}",
                    ref_type="task",
                    ref_id=task_id,
                )
        return TaskResult(task, seq)

    async def _next_number(self, tx: EventTx, crew_id: str) -> int:
        row = await fetchone(tx.conn, "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM crew_tasks WHERE crew_id = ?", (crew_id,))
        return int(row["n"]) if row else 1

    async def patch(self, crew_id: str, task_id: str, caller: Caller, body: Mapping[str, Any], *, if_match: int) -> TaskResult:
        """``PATCH /tasks/{id}`` under If-Match. ``status=done`` → 409 ``report_required``; locked acceptance → human only."""
        if not isinstance(body, Mapping) or not body:
            raise _err(422, "invalid_task", "patch must be a non-empty object")
        unknown = set(body) - PATCHABLE_FIELDS
        if unknown:
            raise _err(422, "invalid_task", f"unknown or read-only fields: {', '.join(sorted(unknown))}")
        if "status" in body:
            if body["status"] == "done":
                raise _err(409, "report_required", "A task is finished by a report (crew_report) or a human waiver.")
            if body["status"] != "cancelled" or len(body) != 1:
                raise _err(
                    422,
                    "invalid_transition",
                    "Status changes use the task actions (claim, start, block, release, review, reopen); PATCH only cancels.",
                )
            return await self.cancel(crew_id, task_id, caller, if_match=if_match)
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if int(task["version"]) != int(if_match):
                raise _err(412, "version_mismatch", f"version mismatch; current version is {task['version']}")
            sets: dict[str, Any] = {}
            changed: list[str] = []
            if "title" in body:
                sets["title"] = _check_text(body["title"], "title", 200, required=True)
            if "body" in body:
                sets["body"] = _check_text(body["body"], "body", 8000)
            if "phase" in body:
                sets["phase"] = _check_text(body["phase"], "phase", 64)
            if "reviewer" in body:
                sets["reviewer"] = _check_text(body["reviewer"], "reviewer", 128)
            if "priority" in body:
                sets["priority"] = _check_priority(body["priority"])
            if "labels" in body:
                sets["labels"] = dumps(_check_labels(body["labels"]))
            if "position" in body:
                if isinstance(body["position"], bool) or not isinstance(body["position"], (int, float)):
                    raise _err(422, "invalid_task", "position must be a number")
                sets["position"] = float(body["position"])
            if "zone_ids" in body:
                if task["status"] not in ("backlog", "ready"):
                    raise _err(409, "task_active", "Zones of a claimed or started task cannot change; release it first.")
                sets["zone_ids"] = dumps(await _check_zone_ids(tx.conn, crew_id, body["zone_ids"]))
            acceptance_changed = False
            if "acceptance" in body:
                new = validate_acceptance(body["acceptance"])
                if task["acceptance_locked"] and not caller.human:
                    raise _err(
                        403,
                        "human_only",
                        "Acceptance criteria are locked once the task is in progress; only a human can change them.",
                    )
                old_waivers = waivers_of(loads(task["acceptance"], []))
                for c in new:
                    if c["id"] in old_waivers:
                        c["waiver"] = old_waivers[c["id"]]
                sets["acceptance"] = dumps(new)
                acceptance_changed = bool(task["acceptance_locked"])
            for key, value in sets.items():
                if value != task.get(key):
                    changed.append(key)
            if not changed:
                return TaskResult(task_detail(task, await task_deps(tx.conn, crew_id, task_id)), None)
            sets["updated_at"] = now_iso()
            assignments = ", ".join(f"{k} = ?" for k in sets)
            await tx.conn.execute(
                f"UPDATE crew_tasks SET {assignments}, version = version + 1 WHERE id = ? AND crew_id = ?",  # noqa: S608
                (*sets.values(), task_id, crew_id),
            )
            if acceptance_changed:
                criteria = loads(sets["acceptance"], [])
                detail, seq = await self._emit_task(
                    tx,
                    crew_id,
                    task_id,
                    "task.acceptance_changed",
                    caller,
                    f"{task_ref(task)} acceptance changed by {caller.label}",
                    criteria_count=len(criteria),
                )
            else:
                detail, seq = await self._emit_task(
                    tx,
                    crew_id,
                    task_id,
                    "task.updated",
                    caller,
                    f"{task_ref(task)} updated by {caller.label}",
                    changed=changed[:20],
                )
        return TaskResult(detail, seq, {"changed": changed, "acceptance_changed_after_lock": acceptance_changed})

    # -- dependencies --------------------------------------------------------------

    async def add_dependency(self, crew_id: str, task_id: str, caller: Caller, depends_on_id: Any) -> TaskResult:
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] in ("done", "cancelled"):
                raise _err(409, "task_closed", f"{task_ref(task)} is {task['status']}.")
            (dep,) = await _check_dep_ids(tx.conn, crew_id, [depends_on_id], self_id=task_id)
            existing = await task_deps(tx.conn, crew_id, task_id)
            if dep in existing:
                return TaskResult(task_detail(task, existing), None)
            if len(existing) >= MAX_DEPENDENCIES:
                raise _err(422, "invalid_task", f"at most {MAX_DEPENDENCIES} dependencies")
            if await _would_cycle(tx.conn, crew_id, task_id, dep):
                raise _err(422, "dependency_cycle", "This dependency would create a cycle.")
            await tx.conn.execute(
                "INSERT INTO crew_task_deps (crew_id, task_id, depends_on_id) VALUES (?, ?, ?)", (crew_id, task_id, dep)
            )
            await tx.conn.execute(
                "UPDATE crew_tasks SET version = version + 1, updated_at = ? WHERE id = ?", (now_iso(), task_id)
            )
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] == "ready" and not await _deps_done(tx.conn, crew_id, [*existing, dep]):
                await tx.conn.execute("UPDATE crew_tasks SET status = 'backlog', version = version + 1 WHERE id = ?", (task_id,))
                await resolve_inbox_items(tx, crew_id, [f"task_ready:{task_id}"], resolved_by="server")
            detail, seq = await self._emit_task(
                tx, crew_id, task_id, "task.deps_changed", caller, f"{task_ref(task)} now depends on {dep}"
            )
        return TaskResult(detail, seq)

    async def remove_dependency(self, crew_id: str, task_id: str, caller: Caller, depends_on_id: str) -> TaskResult:
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            cursor = await tx.conn.execute(
                "DELETE FROM crew_task_deps WHERE crew_id = ? AND task_id = ? AND depends_on_id = ?",
                (crew_id, task_id, depends_on_id),
            )
            if (cursor.rowcount or 0) == 0:
                raise _err(404, "not_found", "Not found.")
            await tx.conn.execute(
                "UPDATE crew_tasks SET version = version + 1, updated_at = ? WHERE id = ?", (now_iso(), task_id)
            )
            if task["status"] == "backlog" and await _deps_done(tx.conn, crew_id, await task_deps(tx.conn, crew_id, task_id)):
                await tx.conn.execute("UPDATE crew_tasks SET status = 'ready', version = version + 1 WHERE id = ?", (task_id,))
                await open_inbox_item(
                    tx,
                    crew_id,
                    audience="crew",
                    kind="task_ready",
                    title=f"{task_ref(task)} is ready to pick up",
                    dedupe_key=f"task_ready:{task_id}",
                    ref_type="task",
                    ref_id=task_id,
                )
            detail, seq = await self._emit_task(
                tx, crew_id, task_id, "task.deps_changed", caller, f"{task_ref(task)} no longer depends on {depends_on_id}"
            )
        return TaskResult(detail, seq)

    # -- owner transitions -----------------------------------------------------------

    async def _claim_in_tx(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], session: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        settings = await crew_settings(tx.conn, crew_id)
        wip = await fetchone(
            tx.conn,
            "SELECT COUNT(*) AS n FROM crew_tasks WHERE crew_id = ? AND owner_session_id = ? AND status IN"
            " ('claimed','in_progress','blocked') AND id != ?",
            (crew_id, session["id"], task["id"]),
        )
        limit = int(settings["wip_per_session"])
        if wip and int(wip["n"]) >= limit:
            raise _err(409, "wip_limit", f"This session already works on {wip['n']} task(s); the WIP limit is {limit}.")
        return await self.claims.claim(tx, crew_id=crew_id, settings=settings, task=task, session=session)

    async def claim(self, crew_id: str, task_id: str, caller: Caller) -> TaskResult:
        """ready → claimed: owner set, every zone claimed (all or nothing)."""
        session = self._require_session(caller)
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] == "claimed" and task["owner_session_id"] == session["id"]:
                return TaskResult(task_detail(task, await task_deps(tx.conn, crew_id, task_id)), None)
            if task["status"] == "backlog":
                raise _err(409, "deps_pending", f"{task_ref(task)} waits for its dependencies to be done.")
            if task["status"] != "ready":
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; only a ready task can be claimed.")
            claims = await self._claim_in_tx(tx, crew_id, task, session)
            detail, seq = await self._set_status(
                tx,
                crew_id,
                task,
                "claimed",
                caller,
                sets={
                    "owner_session_id": session["id"],
                    "owner_user_id": session["user_id"],
                    "owner_agent_id": session["agent_id"],
                },
            )
            await tx.conn.execute(
                "UPDATE crew_sessions SET current_task_id = ? WHERE id = ? AND crew_id = ?", (task_id, session["id"], crew_id)
            )
            await resolve_inbox_items(tx, crew_id, [f"task_ready:{task_id}"], resolved_by=str(session["id"]))
        return TaskResult(detail, seq, {"claims": claims})

    async def start(self, crew_id: str, task_id: str, caller: Caller, *, head: str | None = None) -> TaskResult:
        """ready|claimed → in_progress. Claims the task's zones atomically, anchors ``started_head``, locks acceptance."""
        session = self._require_session(caller)
        if head is not None and not _is_sha_prefix(head):
            raise _err(422, "invalid_head", "head must be a commit sha")
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            claims: list[dict[str, Any]] = []
            if task["status"] == "in_progress" and task["owner_session_id"] == session["id"]:
                return TaskResult(task_detail(task, await task_deps(tx.conn, crew_id, task_id)), None)
            if task["status"] == "backlog":
                raise _err(409, "deps_pending", f"{task_ref(task)} waits for its dependencies to be done.")
            if task["status"] == "ready":
                claims = await self._claim_in_tx(tx, crew_id, task, session)
                await resolve_inbox_items(tx, crew_id, [f"task_ready:{task_id}"], resolved_by=str(session["id"]))
            elif task["status"] == "claimed":
                self._require_owner(task, caller)
            else:
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; it cannot be started.")
            started_head = task.get("started_head") or head or session.get("head_commit")
            sets = {
                "owner_session_id": session["id"],
                "owner_user_id": session["user_id"],
                "owner_agent_id": session["agent_id"],
                "started_head": started_head,
                "started_at": task.get("started_at") or now_iso(),
                "acceptance_locked": 1,
            }
            frm = str(task["status"])
            detail, seq = await self._set_status(tx, crew_id, task, "in_progress", caller, sets=sets)
            await tx.conn.execute(
                "UPDATE crew_sessions SET current_task_id = ? WHERE id = ? AND crew_id = ?", (task_id, session["id"], crew_id)
            )
            await self._transition_hook(tx, crew_id, await load_task(tx.conn, crew_id, task_id), session, frm, "in_progress")
        return TaskResult(detail, seq, {"claims": claims})

    async def block(self, crew_id: str, task_id: str, caller: Caller, reason: Any) -> TaskResult:
        text = _check_text(reason, "reason", 280, required=True)
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            self._require_owner(task, caller)
            if task["status"] != "in_progress":
                raise _err(
                    409, "invalid_transition", f"{task_ref(task)} is {task['status']}; only an in-progress task can block."
                )
            detail, seq = await self._set_status(tx, crew_id, task, "blocked", caller, sets={"blocked_reason": text})
            await open_inbox_item(
                tx,
                crew_id,
                audience="crew",
                kind="task_blocked",
                title=f"{task_ref(task)} is blocked",
                dedupe_key=f"task_blocked:{task_id}",
                ref_type="task",
                ref_id=task_id,
                actor=caller.actor,
            )
        return TaskResult(detail, seq)

    async def unblock(self, crew_id: str, task_id: str, caller: Caller) -> TaskResult:
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            self._require_owner(task, caller)
            if task["status"] != "blocked":
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}, not blocked.")
            detail, seq = await self._set_status(tx, crew_id, task, "in_progress", caller, sets={"blocked_reason": None})
            await resolve_inbox_items(tx, crew_id, [f"task_blocked:{task_id}"], resolved_by=caller.session_id or caller.user_id)
        return TaskResult(detail, seq)

    async def release(self, crew_id: str, task_id: str, caller: Caller, *, baton: bool = True) -> TaskResult:
        """Release a task. Claimed but never started → ``ready`` (claims released). Started and unfinished →
        ``stalled`` with a current stalled report; with ``baton`` (default) the claims stay reserved for pickup."""
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            self._require_owner(task, caller)
            owner = task.get("owner_session_id")
            if task["status"] == "claimed" and not task.get("started_at"):
                await self.claims.release(
                    tx, crew_id=crew_id, task_id=task_id, holder_session_id=owner, baton=False, actor=caller.actor
                )
                detail, seq = await self._set_status(
                    tx,
                    crew_id,
                    task,
                    "ready",
                    caller,
                    sets={"owner_session_id": None, "owner_user_id": None, "owner_agent_id": None},
                )
                await self._clear_current_task(tx, crew_id, owner, task_id)
                await open_inbox_item(
                    tx,
                    crew_id,
                    audience="crew",
                    kind="task_ready",
                    title=f"{task_ref(task)} is ready to pick up",
                    dedupe_key=f"task_ready:{task_id}",
                    ref_type="task",
                    ref_id=task_id,
                    actor=caller.actor,
                )
                return TaskResult(detail, seq)
            if task["status"] not in UNFINISHED_STATUSES:
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; there is nothing to release.")
            detail, seq = await self._stall_in_tx(
                tx,
                crew_id,
                task,
                caller,
                reason="released",
                reserve_reason="baton",
                baton=baton,
                baton_ref=None,
                handoff_id=None,
            )
        return TaskResult(detail, seq)

    async def _clear_current_task(self, tx: EventTx, crew_id: str, session_id: Any, task_id: str) -> None:
        if session_id:
            await tx.conn.execute(
                "UPDATE crew_sessions SET current_task_id = NULL WHERE id = ? AND crew_id = ? AND current_task_id = ?",
                (session_id, crew_id, task_id),
            )

    async def _stall_in_tx(
        self,
        tx: EventTx,
        crew_id: str,
        task: Mapping[str, Any],
        caller: Caller,
        *,
        reason: str,
        reserve_reason: str,
        baton: bool,
        baton_ref: str | None,
        handoff_id: str | None,
    ) -> tuple[dict[str, Any], int]:
        """claimed|in_progress|blocked → stalled; claims reserved (baton) or released; stalled report current."""
        owner = task.get("owner_session_id")
        await self.claims.release(
            tx,
            crew_id=crew_id,
            task_id=str(task["id"]),
            holder_session_id=owner,
            baton=baton,
            reserve_reason=reserve_reason,
            baton_ref=baton_ref,
            actor=caller.actor,
        )
        await synthesize_report(
            tx,
            crew_id,
            task,
            kind="stalled",
            reason=reason,
            actor=caller.actor,
            session_id=owner,
            baton_ref=baton_ref,
            handoff_id=handoff_id,
        )
        task = await load_task(tx.conn, crew_id, str(task["id"]))
        detail, seq = await self._set_status(
            tx,
            crew_id,
            task,
            "stalled",
            caller,
            sets={"status_before_stall": task["status"], "stalled_at": now_iso()},
            event_type="task.stalled",
            extra_payload={"reason": reason[:64]},
        )
        await self._clear_current_task(tx, crew_id, owner, str(task["id"]))
        session = await load_session(tx.conn, crew_id, str(owner)) if owner else None
        await self._transition_hook(
            tx, crew_id, await load_task(tx.conn, crew_id, str(task["id"])), session, str(task["status"]), "stalled"
        )
        await open_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="baton_available",
            title=f"Baton for {task_ref(task)} is waiting for pickup",
            dedupe_key=f"baton:{task['id']}",
            ref_type="task",
            ref_id=str(task["id"]),
            priority=1,
            primary_action="hand_baton",
        )
        return detail, seq

    async def cancel(self, crew_id: str, task_id: str, caller: Caller, *, if_match: int) -> TaskResult:
        """Any non-done → cancelled. A task that reached in_progress keeps the one-current-report invariant."""
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if int(task["version"]) != int(if_match):
                raise _err(412, "version_mismatch", f"version mismatch; current version is {task['version']}")
            if task["status"] in ("done", "cancelled"):
                raise _err(409, "invalid_transition", f"{task_ref(task)} is already {task['status']}.")
            if task.get("owner_session_id") and not (caller.human or caller.session_id == task["owner_session_id"]):
                raise _err(403, "not_task_owner", f"{task_ref(task)} is owned by another session; a human can cancel it.")
            owner = task.get("owner_session_id")
            await self.claims.release(
                tx, crew_id=crew_id, task_id=task_id, holder_session_id=None, baton=False, actor=caller.actor
            )
            if task.get("started_at"):
                current = await fetchone(tx.conn, "SELECT id FROM crew_reports WHERE task_id = ? AND is_current = 1", (task_id,))
                if current is None:
                    await synthesize_report(
                        tx, crew_id, task, kind="partial", reason="cancelled", actor=caller.actor, session_id=owner
                    )
                task = await load_task(tx.conn, crew_id, task_id)
            detail, seq = await self._set_status(tx, crew_id, task, "cancelled", caller)
            await self._clear_current_task(tx, crew_id, owner, task_id)
            await resolve_inbox_items(tx, crew_id, task_inbox_keys(task_id), resolved_by=caller.session_id or caller.user_id)
        return TaskResult(detail, seq)

    async def reopen(self, crew_id: str, task_id: str, caller: Caller) -> TaskResult:
        """done → ready (or backlog while a dependency is not done). The done report stops being current."""
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] != "done":
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; only a done task can be reopened.")
            await supersede_current_report(tx, crew_id, task, "reopened", caller.actor)
            task = await load_task(tx.conn, crew_id, task_id)
            ready = await _deps_done(tx.conn, crew_id, await task_deps(tx.conn, crew_id, task_id))
            await self._set_status(
                tx,
                crew_id,
                task,
                "ready" if ready else "backlog",
                caller,
                sets={
                    "owner_session_id": None,
                    "owner_user_id": None,
                    "owner_agent_id": None,
                    "done_at": None,
                    "current_report_id": None,
                    "started_head": None,
                    "status_before_stall": None,
                },
                event_type="task.reopened",
            )
            task = await load_task(tx.conn, crew_id, task_id)
            detail, seq = task_detail(task, await task_deps(tx.conn, crew_id, task_id)), None
            if ready:
                await open_inbox_item(
                    tx,
                    crew_id,
                    audience="crew",
                    kind="task_ready",
                    title=f"{task_ref(task)} is ready to pick up",
                    dedupe_key=f"task_ready:{task_id}",
                    ref_type="task",
                    ref_id=task_id,
                )
        return TaskResult(detail, seq)

    # -- finishing (used by crew/reports.py) ---------------------------------------------

    async def finish_done(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], report_id: str, caller: Caller
    ) -> tuple[dict[str, Any], int]:
        """→ done: claims released, inbox items resolved, dependents promoted, ``task.done`` emitted (a moment)."""
        owner = task.get("owner_session_id")
        await self.claims.release(
            tx, crew_id=crew_id, task_id=str(task["id"]), holder_session_id=None, baton=False, actor=caller.actor
        )
        task = await load_task(tx.conn, crew_id, str(task["id"]))
        frm = str(task["status"])
        detail, seq = await self._set_status(
            tx,
            crew_id,
            task,
            "done",
            caller,
            sets={"done_at": now_iso(), "current_report_id": report_id, "blocked_reason": None},
            event_type="task.done",
            extra_payload={"report_id": report_id},
        )
        await self._clear_current_task(tx, crew_id, owner, str(task["id"]))
        await resolve_inbox_items(tx, crew_id, task_inbox_keys(str(task["id"])), resolved_by=caller.session_id or caller.user_id)
        session = await load_session(tx.conn, crew_id, str(owner)) if owner else None
        await self._transition_hook(tx, crew_id, await load_task(tx.conn, crew_id, str(task["id"])), session, frm, "done")
        await self._promote_dependents(tx, crew_id, str(task["id"]), caller)
        return detail, seq

    async def to_review(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], report_id: str, caller: Caller
    ) -> tuple[dict[str, Any], int]:
        """→ review with a Needs-you ``review_report`` item."""
        frm = str(task["status"])
        if frm != "review":
            await self._set_status(tx, crew_id, task, "review", caller)
        task = await load_task(tx.conn, crew_id, str(task["id"]))
        detail, seq = await self._emit_task(
            tx,
            crew_id,
            str(task["id"]),
            "task.review_requested",
            caller,
            f"{task_ref(task)} report {report_id} needs review",
            report_id=report_id,
        )
        await open_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="review_report",
            title=f"Review the report for {task_ref(task)}",
            dedupe_key=f"review_report:{task['id']}",
            ref_type="task",
            ref_id=str(task["id"]),
            primary_action="review",
            actor=caller.actor,
        )
        if frm != "review":
            session = (
                await load_session(tx.conn, crew_id, str(task["owner_session_id"])) if task.get("owner_session_id") else None
            )
            await self._transition_hook(tx, crew_id, task, session, frm, "review")
        return detail, seq

    # -- adoption and assignment -----------------------------------------------------------

    async def _authorise_adopt(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], session: Mapping[str, Any], settings: Mapping[str, Any]
    ) -> tuple[str, str | None]:
        """D33: returns ``(baton kind, offer id)`` or raises 403 ``adopt_not_offered``."""
        sid = str(session["id"])
        claims = await fetchall(
            tx.conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND state = 'reserved'",
            (crew_id, task["id"]),
        )
        if any(c.get("reserved_for") == sid for c in claims) or task.get("owner_session_id") == sid:
            return "reserved_for", None
        prev = await load_session(tx.conn, crew_id, str(task["owner_session_id"])) if task.get("owner_session_id") else None
        if (
            prev is not None
            and prev.get("checkout_fp")
            and prev.get("checkout_fp") == session.get("checkout_fp")
            and prev.get("worktree_id") == session.get("worktree_id")
            and settings.get("auto_adopt") == "same_checkout"
        ):
            return "same_checkout", None
        claim_ids = [c["id"] for c in claims]
        offer = None
        if claim_ids:
            marks = ", ".join("?" for _ in claim_ids)
            offer = await fetchone(
                tx.conn,
                f"SELECT * FROM crew_baton_offers WHERE crew_id = ? AND to_session = ? AND used_at IS NULL"  # noqa: S608
                f" AND claim_id IN ({marks}) ORDER BY CASE via WHEN 'human' THEN 0 ELSE 1 END, created_at LIMIT 1",
                (crew_id, sid, *claim_ids),
            )
        if offer is None:
            offer = await fetchone(
                tx.conn,
                "SELECT * FROM crew_baton_offers WHERE crew_id = ? AND to_session = ? AND task_id = ? AND used_at IS NULL"
                " ORDER BY CASE via WHEN 'human' THEN 0 ELSE 1 END, created_at LIMIT 1",
                (crew_id, sid, task["id"]),
            )
        if offer is not None:
            await tx.conn.execute("UPDATE crew_baton_offers SET used_at = ? WHERE id = ?", (now_iso(), offer["id"]))
            return ("reserved_for" if offer["via"] == "reserved_for" else "adopt"), str(offer["id"])
        if settings.get("adopt_on_first_write"):
            return "first_write", None
        raise _err(
            403,
            "adopt_not_offered",
            f"{task_ref(task)} was not offered to this session. Work elsewhere, or ask a human to hand it over.",
        )

    async def adopt(self, crew_id: str, task_id: str, caller: Caller) -> TaskResult:
        """Adopt a stalled task (D33): claims move to the caller with epoch + 1, a baton row and ``baton.passed``."""
        session = self._require_session(caller)
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] != "stalled":
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; only a stalled task is adopted.")
            settings = await crew_settings(tx.conn, crew_id)
            kind, offer_id = await self._authorise_adopt(tx, crew_id, task, session, settings)
            if kind == "reserved_for" and task.get("owner_session_id") == session["id"]:
                return await self._recover_in_tx(tx, crew_id, task, caller)
            await self._check_adopter_zones(tx, crew_id, task_id, session, offer_id)
            prev_id = task.get("owner_session_id")
            prev = await load_session(tx.conn, crew_id, str(prev_id)) if prev_id else None
            cross = not (
                prev is not None
                and prev.get("checkout_fp")
                and prev.get("checkout_fp") == session.get("checkout_fp")
                and prev.get("worktree_id") == session.get("worktree_id")
            )
            await self._check_wip(tx, crew_id, session, task_id, settings)
            claims = await self.claims.adopt(
                tx,
                crew_id=crew_id,
                settings=settings,
                task_id=task_id,
                to_session=session,
                from_session_id=prev_id,
                cross_checkout=cross,
                source="adopt",
                actor=caller.actor,
            )
            baton = await self._record_baton(tx, crew_id, task, prev_id, session, kind, offer_id, claims, caller)
            detail, seq = await self._set_status(
                tx,
                crew_id,
                task,
                "claimed",
                caller,
                sets={
                    "owner_session_id": session["id"],
                    "owner_user_id": session["user_id"],
                    "owner_agent_id": session["agent_id"],
                    "status_before_stall": None,
                    "stalled_at": None,
                },
            )
            await tx.conn.execute(
                "UPDATE crew_sessions SET current_task_id = ? WHERE id = ? AND crew_id = ?", (task_id, session["id"], crew_id)
            )
            await resolve_inbox_items(tx, crew_id, baton_inbox_keys(task_id), resolved_by=str(session["id"]))
        return TaskResult(detail, seq, {"claims": claims, "baton": baton, "cross_checkout": cross})

    async def _check_adopter_zones(
        self, tx: EventTx, crew_id: str, task_id: str, session: Mapping[str, Any], offer_id: str | None
    ) -> None:
        """A task baton does not bypass its zones (§5.1): reserve_for, protected (human grant), frozen, crew-policy."""
        from remembra.crew.claims import check_taker_zones
        from remembra.crew.zones import CrewOpError

        claims = await fetchall(
            tx.conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND holder_kind = 'session'"
            " AND state IN ('active','offered','reserved')",
            (crew_id, task_id),
        )
        offer = await fetchone(tx.conn, "SELECT via FROM crew_baton_offers WHERE id = ?", (offer_id,)) if offer_id else None
        granted = (offer is not None and offer["via"] == "human") or any(c.get("reserved_for") == session["id"] for c in claims)
        try:
            await check_taker_zones(tx.conn, claims, session, human_granted=granted)
        except CrewOpError as e:
            raise _err(e.status, e.error, e.message)

    async def _check_wip(
        self, tx: EventTx, crew_id: str, session: Mapping[str, Any], task_id: str, settings: Mapping[str, Any]
    ) -> None:
        wip = await fetchone(
            tx.conn,
            "SELECT COUNT(*) AS n FROM crew_tasks WHERE crew_id = ? AND owner_session_id = ? AND status IN"
            " ('claimed','in_progress','blocked') AND id != ?",
            (crew_id, session["id"], task_id),
        )
        limit = int(settings["wip_per_session"])
        if wip and int(wip["n"]) >= limit:
            raise _err(409, "wip_limit", f"This session already works on {wip['n']} task(s); the WIP limit is {limit}.")

    async def _record_baton(
        self,
        tx: EventTx,
        crew_id: str,
        task: Mapping[str, Any],
        from_session: Any,
        to_session: Mapping[str, Any],
        kind: str,
        offer_id: str | None,
        claims: Sequence[Mapping[str, Any]],
        caller: Caller,
    ) -> dict[str, Any]:
        report = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task["id"],))
        _facts, checkpoint_id = await last_checkpoint_facts(tx.conn, crew_id, str(from_session) if from_session else None)
        baton_ref = (report or {}).get("baton_ref") or next((c.get("baton_ref") for c in claims if c.get("baton_ref")), None)
        baton_id = new_id("baton")
        zone_ids = sorted({str(c["zone_id"]) for c in claims if c.get("zone_id")})
        payload = {
            "baton_id": baton_id,
            "task_id": task["id"],
            "from_session": from_session,
            "to_session": to_session["id"],
            "kind": kind,
            "handoff_id": (report or {}).get("handoff_id"),
            "zones": zone_ids[:20],
            "baton_ref": baton_ref,
            "restored": None,
        }
        result = await tx.emit(
            crew_id=crew_id,
            type="baton.passed",
            actor=caller.actor,
            payload=payload,
            summary=f"baton for {task_ref(task)} passed to {to_session['callsign']} ({kind})",
            refs={"task_id": task["id"], "session_id": to_session["id"], "report_id": (report or {}).get("id")},
        )
        await tx.conn.execute(
            """INSERT INTO crew_batons (id, crew_id, task_id, from_session, to_session, kind, offer_id, handoff_id,
                   checkpoint_id, report_id, baton_ref, restored, zone_ids, brief_text, seq, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)""",
            (
                baton_id,
                crew_id,
                task["id"],
                from_session,
                to_session["id"],
                kind,
                offer_id,
                (report or {}).get("handoff_id"),
                checkpoint_id,
                (report or {}).get("id"),
                baton_ref,
                dumps(zone_ids),
                result.seq,
                now_iso(),
            ),
        )
        return {**payload, "seq": result.seq}

    async def assign(self, crew_id: str, task_id: str, caller: Caller, to_session_id: Any) -> TaskResult:
        """(H) Hand a task to a live session: overrides dependencies, moves or grants its claims (``human_assign``)."""
        if not caller.human:
            raise _err(403, "human_only", "Only a human can assign a task.")
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if not schemas.is_id("session", to_session_id):
                raise _err(422, "cross_crew_reference", "The referenced session is not part of this crew.")
            target = await load_session(tx.conn, crew_id, to_session_id)
            if target is None:
                raise _err(422, "cross_crew_reference", "The referenced session is not part of this crew.")
            if target["state"] in ("ended", "lost"):
                raise _err(409, "session_not_live", f"{target['callsign']} is {target['state']}.")
            if task["status"] in ("done", "cancelled"):
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}.")
            settings = await crew_settings(tx.conn, crew_id)
            prev_id = task.get("owner_session_id")
            live_claims = await fetchall(
                tx.conn,
                "SELECT id FROM crew_claims WHERE crew_id = ? AND task_id = ? AND state IN ('active','offered','reserved')",
                (crew_id, task_id),
            )
            if live_claims:
                claims = await self.claims.adopt(
                    tx,
                    crew_id=crew_id,
                    settings=settings,
                    task_id=task_id,
                    to_session=target,
                    from_session_id=prev_id,
                    cross_checkout=True,
                    source="dashboard",
                    actor=caller.actor,
                )
            else:
                claims = await self.claims.claim(
                    tx, crew_id=crew_id, settings=settings, task=task, session=target, actor=caller.actor
                )
            baton = None
            if prev_id and prev_id != target["id"]:
                baton = await self._record_baton(tx, crew_id, task, prev_id, target, "human_assign", None, claims, caller)
                await self._clear_current_task(tx, crew_id, prev_id, task_id)
            to_status = task["status"] if task["status"] in ("in_progress", "blocked", "review") else "claimed"
            if to_status == "claimed" and task["status"] == "claimed" and prev_id == target["id"]:
                return TaskResult(task_detail(task, await task_deps(tx.conn, crew_id, task_id)), None)
            sets = {
                "owner_session_id": target["id"],
                "owner_user_id": target["user_id"],
                "owner_agent_id": target["agent_id"],
                "status_before_stall": None,
                "stalled_at": None,
            }
            if to_status != task["status"]:
                await self._set_status(tx, crew_id, task, to_status, caller, sets=sets)
            else:
                assignments = ", ".join(f"{k} = ?" for k in sets)
                await tx.conn.execute(
                    f"UPDATE crew_tasks SET {assignments}, version = version + 1, updated_at = ? WHERE id = ?",  # noqa: S608
                    (*sets.values(), now_iso(), task_id),
                )
            detail, seq = await self._emit_task(
                tx,
                crew_id,
                task_id,
                "task.assigned",
                caller,
                f"{task_ref(task)} assigned to {target['callsign']} by human",
                to_session=target["id"],
            )
            await tx.conn.execute(
                "UPDATE crew_sessions SET current_task_id = ? WHERE id = ? AND crew_id = ?", (task_id, target["id"], crew_id)
            )
            await resolve_inbox_items(tx, crew_id, [f"task_ready:{task_id}", f"baton:{task_id}"], resolved_by=caller.user_id)
        return TaskResult(detail, seq, {"claims": claims, "baton": baton})

    # -- lost / quota / recovery (WP-4 seam) ------------------------------------------------------

    async def stall_session_tasks(
        self,
        tx: EventTx,
        crew_id: str,
        session_id: str,
        *,
        reason: str,
        reserve_reason: str,
        baton_ref: str | None = None,
        handoff_id: str | None = None,
    ) -> list[str]:
        """Inside the caller's transaction: every unfinished task of ``session_id`` → stalled (§10.2 "On lost").

        Claims are reserved for the session (``reserve_reason`` = lost | quota | ended_dirty | offline), a
        ``server-inferred`` stalled report becomes current (with ``baton_ref`` / ``handoff_id``) and a
        Needs-you ``baton_available`` item opens. Returns the stalled task ids.
        """
        rows = await fetchall(
            tx.conn,
            "SELECT * FROM crew_tasks WHERE crew_id = ? AND owner_session_id = ?"
            " AND status IN ('claimed','in_progress','blocked')",
            (crew_id, session_id),
        )
        out: list[str] = []
        for task in rows:
            await self._stall_in_tx(
                tx,
                crew_id,
                task,
                Caller.server(),
                reason=reason,
                reserve_reason=reserve_reason,
                baton=True,
                baton_ref=baton_ref,
                handoff_id=handoff_id,
            )
            out.append(str(task["id"]))
        return out

    async def _recover_in_tx(self, tx: EventTx, crew_id: str, task: Mapping[str, Any], caller: Caller) -> TaskResult:
        settings = await crew_settings(tx.conn, crew_id)
        owner = str(task["owner_session_id"])
        claims = await self.claims.retake(tx, crew_id=crew_id, settings=settings, task_id=str(task["id"]), session_id=owner)
        superseded: list[str] = []
        current = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task["id"],))
        if current is not None and current["kind"] == "stalled" and current["facts_source"] == "server-inferred":
            rid = await supersede_current_report(tx, crew_id, task, "recovered", caller.actor)
            if rid:
                superseded.append(rid)
                await tx.conn.execute("UPDATE crew_tasks SET current_report_id = NULL WHERE id = ?", (task["id"],))
        task = await load_task(tx.conn, crew_id, str(task["id"]))
        restored = str(task.get("status_before_stall") or "in_progress")
        if restored not in UNFINISHED_STATUSES:
            restored = "in_progress"
        await self._set_status(tx, crew_id, task, restored, caller, sets={"status_before_stall": None, "stalled_at": None})
        detail, seq = await self._emit_task(
            tx, crew_id, str(task["id"]), "task.recovered", caller, f"{task_ref(task)} recovered by its owner"
        )
        await tx.conn.execute(
            "UPDATE crew_sessions SET current_task_id = ? WHERE id = ? AND crew_id = ?", (task["id"], owner, crew_id)
        )
        await resolve_inbox_items(tx, crew_id, [f"baton:{task['id']}"], resolved_by=owner)
        return TaskResult(detail, seq, {"claims_retaken": claims, "superseded_report_ids": superseded})

    async def recover_session_tasks(self, tx: EventTx, crew_id: str, session_id: str) -> dict[str, list[str]]:
        """Inside the caller's transaction: the session is active again and nothing was adopted (§10.2 "On recovery").

        Tasks it still owns in ``stalled`` return to ``status_before_stall``; their claims are re-taken with
        epoch + 1; a synthesized stalled report stops being current (``superseded_reason='recovered'``); baton
        inbox items resolve. Returns ``{tasks_restored, claims_retaken, superseded_report_ids}`` for
        ``session.recovered``.
        """
        rows = await fetchall(
            tx.conn,
            "SELECT * FROM crew_tasks WHERE crew_id = ? AND owner_session_id = ? AND status = 'stalled'",
            (crew_id, session_id),
        )
        out: dict[str, list[str]] = {"tasks_restored": [], "claims_retaken": [], "superseded_report_ids": []}
        for task in rows:
            result = await self._recover_in_tx(tx, crew_id, task, Caller.server())
            out["tasks_restored"].append(str(task["id"]))
            out["claims_retaken"].extend(result.extra["claims_retaken"])
            out["superseded_report_ids"].extend(result.extra["superseded_report_ids"])
        return out
