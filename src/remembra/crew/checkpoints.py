"""Crew checkpoints: ingest, the ``task`` transition checkpoint and counted memory promotion (WP-6, spec §5.5, D15).

A checkpoint is a ``crew_checkpoints`` row with no embedding. Facts (≤16 KB) go
through the single redaction choke point (:func:`remembra.crew.redact.outbound`)
before they are stored, hashed or promoted. ``(session_id, facts_hash)`` is the
natural idempotency key: a replay returns the stored checkpoint and writes nothing.

Facts vocabulary the report gate reads (the relay close-out keys, crew form):

* ``branch``, ``head`` / ``head_commit``;
* ``commits``: ``[{sha, …}]`` or ``[sha]``;
* ``uncommitted_files`` / ``dirty`` / ``files_changed``: repo-relative paths;
* ``tests``: ``[{command | cmd | fingerprint, passed: int | bool, failed?: int, exit_code?}]``
  (redacted test-runner fingerprints, §11);
* ``commands``: ``[{fingerprint, exit_code}]`` (fingerprints only; raw commands never leave the host);
* ``unpushed_commits`` (int) / ``pushed`` (bool), ``upstream``;
* ``todos_open``, ``errors``, ``diff_stat``, ``baton_ref``.

Who may submit which trigger: sessions submit ``turn, commit, push, test,
interval, precompact, quota, close``; ``task`` and ``claim`` checkpoints are
recorded by the server on transitions and ``lost`` by the reaper (WP-4) through
:meth:`CheckpointService.record_in_tx`. The facts source is decided by the
server, never by the body: hook and CLI sessions are ``relay-cli``, MCP sessions
``agent-declared``, server-recorded ones ``server-inferred``.

Promotion to memory (D15), through the crew outbox (D35):

* ``quota`` and ``lost`` always promote (they still count);
* ``close`` promotes unless the session was shorter than 2 min or recorded no commits (coalesced);
* ``task`` promotes on transitions that finish work (review, done, stalled);
* every other trigger promotes at most once per session per 60 min;
* every promotion counts against ``crew_memory_promotions_per_day`` of the crew
  owner's plan; over the cap it is queued for the next UTC day
  (``remembra.crew.limits.promotion_decision``).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.events import CrewEventLog, EventTx
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, CrewLimits, promotion_decision
from remembra.crew.redact import outbound
from remembra.crew.store import CrewStore, dumps, loads, new_id, now_iso, parse_iso
from remembra.crew.tasks import Caller, CrewServiceError, crew_row, crew_settings, fetchall, fetchone, session_channel_source

CLIENT_TRIGGERS: Final = ("turn", "commit", "push", "test", "interval", "precompact", "quota", "close")
SERVER_TRIGGERS: Final = ("task", "claim", "lost")
ALWAYS_ELIGIBLE: Final = frozenset({"quota", "lost", "close", "task"})
PROMOTION_SPACING_S: Final = 3600
CLOSE_MIN_SESSION_S: Final = 120
TASK_PROMOTE_TO: Final = frozenset({"review", "done", "stalled"})
LIMITS_TTL_S: Final = 300
MAX_PROMOTION_CHARS: Final = 4000

LimitsResolver = Callable[[str], Awaitable[CrewLimits]]
Scrubber = Callable[[str], str]


async def _self_hosted(_owner: str) -> CrewLimits:
    return SELF_HOSTED_CREW_LIMITS


def _err(status: int, error: str, message: str) -> CrewServiceError:
    return CrewServiceError(status, error, message)


def checkpoint_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "task_id": row.get("task_id"),
        "trigger": row["trigger"],
        "headline": str(row.get("headline") or "")[:200],
        "facts_source": row["facts_source"],
    }


def checkpoint_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    view = checkpoint_view(row)
    view.update(
        {
            "crew_id": row["crew_id"],
            "facts": loads(row.get("facts"), {}) if row.get("facts") is not None else None,
            "facts_hash": row["facts_hash"],
            "compacted": bool(row.get("compacted")),
            "seq": row.get("seq"),
            "created_at": row["created_at"],
        }
    )
    return view


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def test_counts(facts: Mapping[str, Any]) -> tuple[int, int]:
    """(passing runs, failing runs) in the facts' ``tests`` list."""
    ok = bad = 0
    for t in facts.get("tests") or []:
        if not isinstance(t, Mapping):
            continue
        failed = t.get("failed")
        passed = t.get("passed")
        exit_code = t.get("exit_code")
        if (
            passed is False
            or (isinstance(failed, int) and not isinstance(failed, bool) and failed > 0)
            or exit_code not in (None, 0)
        ):
            bad += 1
        else:
            ok += 1
    return ok, bad


def headline(callsign: str, trigger: str, task_number: int | None, facts: Mapping[str, Any]) -> str:
    """Server template (ids, callsigns and counts only; no free text)."""
    dirty = facts.get("uncommitted_files") or facts.get("dirty") or []
    ok, bad = test_counts(facts)
    parts = [f"{callsign} {trigger}"]
    if task_number is not None:
        parts.append(f"T-{task_number}")
    text = " ".join(parts) + f": {_count(facts.get('commits'))} commits, {_count(dirty)} dirty"
    if ok or bad:
        text += f", tests {ok} pass {bad} fail"
    unpushed = facts.get("unpushed_commits")
    if isinstance(unpushed, int) and not isinstance(unpushed, bool):
        text += f", {unpushed} unpushed"
    return text[:200]


def facts_hash(trigger: str, task_id: str | None, facts: Mapping[str, Any]) -> str:
    return hashlib.sha256(schemas.canonical_json({"trigger": trigger, "task_id": task_id, "facts": facts})).hexdigest()


def promotion_text(
    *, callsign: str, agent_id: str, project_id: str, trigger: str, task_number: int | None, facts: Mapping[str, Any], source: str
) -> str:
    """The memory a promoted checkpoint becomes (deterministic, from facts only)."""
    lines = [f"[CHECKPOINT] {callsign} ({agent_id}) on project {project_id}, trigger {trigger}"]
    if task_number is not None:
        lines[0] += f", task T-{task_number}"
    branch = facts.get("branch")
    head = facts.get("head") or facts.get("head_commit")
    if branch or head:
        lines.append(f"Where: {branch or '(detached)'} @ {str(head or '?')[:12]}")
    commits = [c.get("sha") if isinstance(c, Mapping) else c for c in facts.get("commits") or []]
    if commits:
        lines.append(f"Commits ({len(commits)}): " + ", ".join(str(c)[:12] for c in commits[:10]))
    dirty = facts.get("uncommitted_files") or facts.get("dirty") or []
    if dirty:
        lines.append(f"Uncommitted ({len(dirty)}): " + ", ".join(str(p) for p in dirty[:20]))
    for t in (facts.get("tests") or [])[:10]:
        if isinstance(t, Mapping):
            cmd = t.get("command") or t.get("cmd") or t.get("fingerprint") or "tests"
            lines.append(f"Test: {str(cmd)[:120]} passed={t.get('passed')} failed={t.get('failed')}")
    todos = facts.get("todos_open") or []
    if todos:
        lines.append("Open todos: " + "; ".join(str(t)[:120] for t in todos[:10]))
    if isinstance(facts.get("unpushed_commits"), int):
        lines.append(f"Unpushed commits: {facts['unpushed_commits']}")
    lines.append(f"Facts: {source}.")
    return "\n".join(lines)[:MAX_PROMOTION_CHARS]


@dataclass(frozen=True)
class CheckpointResult:
    checkpoint: dict[str, Any]
    created: bool
    seq: int | None
    promotion: str  # promoted | deferred | skipped:<reason> | replay


class CheckpointService:
    """Checkpoint ingest and promotion over ``crew.db`` (see the module docstring)."""

    def __init__(
        self,
        events: CrewEventLog,
        *,
        limits_resolver: LimitsResolver | None = None,
        pii: Scrubber | None = None,
        outbox_wake: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.events = events
        self.limits_resolver = limits_resolver or _self_hosted
        self.pii = pii
        self.outbox_wake = outbox_wake
        self.clock = clock or (lambda: datetime.now(UTC))
        self._limits: dict[str, tuple[CrewLimits, float]] = {}

    async def limits_for(self, owner_user_id: str) -> CrewLimits:
        cached = self._limits.get(owner_user_id)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        limits = await self.limits_resolver(owner_user_id)
        self._limits[owner_user_id] = (limits, time.monotonic() + LIMITS_TTL_S)
        return limits

    async def prime(self, crew_id: str) -> None:
        """Resolve the crew owner's plan limits before a transaction opens (main-DB reads stay outside crew locks)."""
        crew = await crew_row(self.events.db.conn, crew_id)
        await self.limits_for(str(crew["owner_user_id"]))

    # -- ingest ----------------------------------------------------------------

    def clean_facts(self, facts: Any) -> dict[str, Any]:
        if not isinstance(facts, Mapping):
            raise _err(422, "invalid_facts", "facts must be a JSON object")
        size = schemas.json_size(facts)
        if size > schemas.MAX_CHECKPOINT_FACTS_BYTES:
            raise _err(413, "facts_too_large", f"facts are {size} bytes; the limit is {schemas.MAX_CHECKPOINT_FACTS_BYTES}")
        cleaned = outbound("checkpoint", dict(facts), pii=self.pii)
        assert isinstance(cleaned, dict)
        return cleaned

    async def ingest(self, crew_id: str, caller: Caller, body: Mapping[str, Any]) -> CheckpointResult:
        """``POST /crews/{id}/checkpoints`` for the caller's own session."""
        if isinstance(body.get("facts"), Mapping):
            self.clean_facts(body["facts"])  # 413 before the generic shape error for oversize facts
        errors = schemas.validate(dict(body), schemas.REQUEST_SHAPES["Checkpoint"], "$")
        if errors:
            raise _err(422, "invalid_checkpoint", "; ".join(errors[:3]))
        session = caller.session
        if session is None:
            raise _err(403, "session_required", "Checkpoints are submitted by a crew session (send its session token).")
        if body["session_id"] != session["id"]:
            raise _err(403, "session_mismatch", "The session token does not belong to session_id.")
        if session.get("state") == "ended":
            raise _err(409, "session_ended", "This crew session has ended.")
        trigger = str(body["trigger"])
        if trigger not in CLIENT_TRIGGERS:
            raise _err(422, "server_trigger", f"trigger {trigger} is recorded by the server, not submitted.")
        facts = self.clean_facts(body["facts"])
        await self.prime(crew_id)
        async with self.events.transaction() as tx:
            task_id = body.get("task_id")
            if task_id is not None:
                row = await fetchone(tx.conn, "SELECT id FROM crew_tasks WHERE id = ? AND crew_id = ?", (task_id, crew_id))
                if row is None:
                    raise _err(422, "cross_crew_reference", "The referenced task is not part of this crew.")
            fresh = await fetchone(tx.conn, "SELECT * FROM crew_sessions WHERE id = ? AND crew_id = ?", (session["id"], crew_id))
            if fresh is None:
                raise _err(404, "not_found", "Not found.")
            result = await self.record_in_tx(
                tx, crew_id, fresh, trigger=trigger, facts=facts, task_id=task_id, facts_source=session_channel_source(fresh)
            )
            if result.created and fresh.get("client_kind") == "mcp":
                await self._mcp_footprints(tx, crew_id, fresh, facts)
            return result

    async def _mcp_footprints(self, tx: EventTx, crew_id: str, session: Mapping[str, Any], facts: Mapping[str, Any]) -> None:
        """An MCP-only session has no crewd heartbeat: the files its checkpoint declares are its footprints (§5.3).

        They are the session's own statement about its own writes (``certain``, state ``dirty``), so
        collisions with other sessions are detected for MCP agents too (G2 "after the fact").
        Paths that are not repo-relative are skipped, as the heartbeat skips them.
        """
        from remembra.crew.collisions import MAX_FOOTPRINTS_PER_CALL, heartbeat_footprints, record_footprints
        from remembra.crew.zones import CrewOps

        declared = facts.get("files_changed")
        if not isinstance(declared, list) or not declared:
            return
        raw = [{"path": p, "state": "dirty", "attribution": "certain"} for p in declared if isinstance(p, str)]
        footprints = heartbeat_footprints(raw[:MAX_FOOTPRINTS_PER_CALL])
        if footprints:
            await record_footprints(CrewOps(self.events), tx, crew_id, session, footprints)

    async def record_in_tx(
        self,
        tx: EventTx,
        crew_id: str,
        session: Mapping[str, Any],
        *,
        trigger: str,
        facts: Mapping[str, Any],
        task_id: str | None,
        facts_source: str,
        promote: bool = True,
    ) -> CheckpointResult:
        """Store one checkpoint (already-redacted ``facts``) inside the caller's transaction, emit
        ``checkpoint.created`` and queue its memory promotion when D15 allows it."""
        if trigger not in schemas.CHECKPOINT_TRIGGERS:
            raise _err(422, "invalid_checkpoint", f"unknown trigger {trigger}")
        if facts_source not in schemas.FACTS_SOURCES:
            raise _err(422, "invalid_checkpoint", f"unknown facts source {facts_source}")
        digest = facts_hash(trigger, task_id, facts)
        existing = await fetchone(
            tx.conn, "SELECT * FROM crew_checkpoints WHERE session_id = ? AND facts_hash = ?", (session["id"], digest)
        )
        if existing is not None:
            return CheckpointResult(checkpoint_detail(existing), False, existing.get("seq"), "replay")
        task = (
            await fetchone(tx.conn, "SELECT id, number FROM crew_tasks WHERE id = ? AND crew_id = ?", (task_id, crew_id))
            if task_id
            else None
        )
        number = int(task["number"]) if task else None
        now_dt = self.clock()
        now = now_iso(now_dt)
        checkpoint_id = new_id("checkpoint")
        text = headline(str(session["callsign"]), trigger, number, facts)
        await tx.conn.execute(
            """INSERT INTO crew_checkpoints (id, crew_id, session_id, task_id, trigger, facts, facts_hash, headline,
                   facts_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (checkpoint_id, crew_id, session["id"], task_id, trigger, dumps(dict(facts)), digest, text, facts_source, now),
        )
        settings = await crew_settings(tx.conn, crew_id)
        due = now_iso(now_dt + timedelta(seconds=int(settings["checkpoint"]["interval_s"])))
        await tx.conn.execute(
            "UPDATE crew_sessions SET last_checkpoint_id = ?, calls_since_checkpoint = 0, next_checkpoint_due_at = ?,"
            " checkpoint_streak = checkpoint_streak + 1 WHERE id = ? AND crew_id = ?",
            (checkpoint_id, due, session["id"], crew_id),
        )
        row = await fetchone(tx.conn, "SELECT * FROM crew_checkpoints WHERE id = ?", (checkpoint_id,))
        assert row is not None
        result = await tx.emit(
            crew_id=crew_id,
            type="checkpoint.created",
            actor=Caller.for_session(session).actor if facts_source != "server-inferred" else Caller.server().actor,
            payload={"checkpoint": checkpoint_view(row)},
            summary=text,
            refs={"session_id": session["id"], "task_id": task_id},
        )
        await tx.conn.execute("UPDATE crew_checkpoints SET seq = ? WHERE id = ?", (result.seq, checkpoint_id))
        row["seq"] = result.seq
        outcome = "skipped:disabled"
        if promote:
            outcome = await self._maybe_promote(tx, crew_id, session, row, facts, trigger, number, facts_source, now_dt)
        return CheckpointResult(checkpoint_detail(row), True, result.seq, outcome)

    # -- promotion (D15) ------------------------------------------------------------

    async def _maybe_promote(
        self,
        tx: EventTx,
        crew_id: str,
        session: Mapping[str, Any],
        row: Mapping[str, Any],
        facts: Mapping[str, Any],
        trigger: str,
        task_number: int | None,
        facts_source: str,
        now_dt: datetime,
    ) -> str:
        sid = str(session["id"])
        if trigger == "close":
            joined = parse_iso(str(session["joined_at"])) if session.get("joined_at") else now_dt
            if (now_dt - joined).total_seconds() < CLOSE_MIN_SESSION_S:
                return "skipped:short_session"
            if not facts.get("commits"):
                return "skipped:no_commits"
        elif trigger not in ALWAYS_ELIGIBLE:
            since = now_iso(now_dt - timedelta(seconds=PROMOTION_SPACING_S))
            recent = await fetchone(
                tx.conn,
                "SELECT id FROM crew_outbox WHERE crew_id = ? AND kind = 'memory_promotion' AND created_at >= ?"
                " AND json_extract(payload, '$.metadata.session_id') = ?"
                " AND json_extract(payload, '$.metadata.checkpoint_trigger') NOT IN ('quota','lost','close','task') LIMIT 1",
                (crew_id, since, sid),
            )
            if recent is not None:
                return "skipped:spacing"
        crew = await crew_row(tx.conn, crew_id)
        limits = await self.limits_for(str(crew["owner_user_id"]))
        today = now_dt.date().isoformat()
        used = await fetchone(
            tx.conn,
            "SELECT COUNT(*) AS n FROM crew_outbox WHERE crew_id = ? AND kind = 'memory_promotion'"
            " AND json_extract(payload, '$.metadata.promotion_day') = ?",
            (crew_id, today),
        )
        decision = promotion_decision(int(used["n"]) if used else 0, trigger, limits)
        day = today if decision.promote else (now_dt.date() + timedelta(days=1)).isoformat()
        content = promotion_text(
            callsign=str(session["callsign"]),
            agent_id=str(session["agent_id"]),
            project_id=str(crew["project_id"]),
            trigger=trigger,
            task_number=task_number,
            facts=facts,
            source=facts_source,
        )
        content = str(outbound("promotion", content, pii=self.pii))
        payload = {
            "user_id": crew["owner_user_id"],
            "project_id": crew["project_id"],
            "memory_type": "checkpoint",
            "content": content,
            "metadata": {
                "checkpoint_id": row["id"],
                "session_id": sid,
                "task_id": row.get("task_id"),
                "checkpoint_trigger": trigger,
                "promotion_day": day,
                "facts_source": facts_source,
                "callsign": session["callsign"],
                "agent_id": session["agent_id"],
            },
        }
        store = CrewStore(self.events.db)  # type: ignore[arg-type]
        outbox_id = await store.enqueue_outbox(crew_id, "memory_promotion", payload, dedupe_key=f"checkpoint:{row['id']}")
        if not decision.promote:
            next_day = datetime.combine(now_dt.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            await tx.conn.execute(
                "UPDATE crew_outbox SET next_attempt_at = ? WHERE id = ? AND state = 'pending'", (now_iso(next_day), outbox_id)
            )
            return "deferred"
        if self.outbox_wake is not None:
            wake = self.outbox_wake

            async def _wake() -> None:
                wake()

            self.events.db.after_commit(_wake)
        return "promoted"

    async def on_task_transition(
        self, tx: EventTx, crew_id: str, task: Mapping[str, Any], session: Mapping[str, Any], frm: str, to: str
    ) -> None:
        """The ``task`` trigger checkpoint (§5.5 "server transition: always"); promoted when work finishes."""
        facts = {
            "task": {"id": task["id"], "number": int(task["number"]), "from": frm, "to": to},
            "started_head": task.get("started_head"),
        }
        await self.record_in_tx(
            tx,
            crew_id,
            session,
            trigger="task",
            facts=facts,
            task_id=str(task["id"]),
            facts_source="server-inferred",
            promote=to in TASK_PROMOTE_TO,
        )

    # -- reads -------------------------------------------------------------------------

    async def list_checkpoints(
        self, crew_id: str, *, session_id: str | None = None, task_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM crew_checkpoints WHERE crew_id = ?"
        params: list[Any] = [crew_id]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [checkpoint_detail(r) for r in await fetchall(self.events.db.conn, sql, params)]
