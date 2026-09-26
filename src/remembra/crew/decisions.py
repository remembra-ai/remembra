"""Crew decisions ``D-n``: proposed by agents, in force only when a human says so (spec §5.7, D21, D36).

* A decision created by a **human** (dashboard JWT) is ``in_force`` immediately
  (``decision.confirmed``) and is mirrored to memory through the crew outbox.
* A decision created by an **agent** (``crew_say kind=decision`` or ``POST
  /crews/{id}/decisions`` with a session) is ``proposed`` (``decision.proposed``).
  It raises a Needs-you ``decision_to_confirm`` item (agent-originated:
  coalesced per session and capped, :mod:`remembra.crew.inbox`), is **never**
  listed under "Decisions in force", never injected into agents and never
  mirrored to memory until a human confirms it.
* ``confirm`` / ``reject`` / ``supersede`` are human-only (the routes require the
  human principal; this module also refuses a non-human author).

Mirroring goes through ``crew_outbox`` (kind ``memory_promotion``, one per
decision, deduplicated by decision id) and counts against the crew's
``crew_memory_promotions_per_day``; over the cap it is deferred to the next UTC
day (D15). Every string an agent wrote stays data: briefs render it through
:func:`brief_lines`, clipped and neutralised for the ``<remembra-data>`` block.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.events import CrewEventLog, EventTx
from remembra.crew.inbox import (
    Author,
    CrewInbox,
    InboxError,
    NotAllowed,
    ValidationFailed,
    agent_dedupe_key,
    require_same_crew,
)
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, CrewLimits, promotion_decision
from remembra.crew.outbox import KIND_MEMORY_PROMOTION
from remembra.crew.store import CrewStore, new_id, now_iso
from remembra.security.secrets import scrub

MAX_TITLE: Final = 200
MAX_DECISION: Final = 2000
MAX_RATIONALE: Final = 2000
MAX_ALTERNATIVES: Final = 10
MAX_ALTERNATIVE: Final = 280
# Rider (gap analysis §7): what the decision rests on (commit shas, file paths, test names, links).
MAX_EVIDENCE: Final = 10
MAX_EVIDENCE_ITEM: Final = 280
NEEDS_YOU_KIND: Final = "decision_to_confirm"


class DecisionNotFound(InboxError):
    status = 404
    error = "not_found"


class DecisionStateConflict(InboxError):
    status = 409
    error = "decision_state"


@dataclass(frozen=True)
class CrewRef:
    """The crew a decision belongs to (from ``load_crew``)."""

    id: str
    owner_user_id: str
    project_id: str


def _flat(text: Any, limit: int, field: str, *, required: bool = True) -> str | None:
    if text is None or (isinstance(text, str) and not text.strip()):
        if required:
            raise ValidationFailed(f"{field} is required")
        return None
    if not isinstance(text, str):
        raise ValidationFailed(f"{field} must be a string")
    value = text.strip()
    if len(value) > limit:
        raise ValidationFailed(f"{field} is longer than {limit} characters")
    return scrub(value)  # credentials never reach the table or the event log


def title_from_body(body: str) -> str:
    """Title of a decision posted as a channel message: its first non-empty line, clipped."""
    for line in body.splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= MAX_TITLE else line[: MAX_TITLE - 1] + "…"
    return "Decision"


def decision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``DecisionView`` (schemas) of a ``crew_decisions`` row."""
    return {
        "id": row["id"],
        "number": int(row["number"]),
        "title": row["title"] or "",
        "decision": row["decision"] or "",
        "state": row["state"],
        "source": row["source"] or "direct",
        "decided_by_kind": row["decided_by_kind"] or "agent",
        "decided_by": row["decided_by"] or "",
        "confirmed_by": row["confirmed_by"],
        "task_id": row["task_id"],
        "zone_id": row["zone_id"],
        "supersedes_id": row["supersedes_id"],
    }


def decision_api(row: Mapping[str, Any]) -> dict[str, Any]:
    out = decision_view(row)
    out.update(
        ref=f"D-{int(row['number'])}",
        crew_id=row["crew_id"],
        rationale=row["rationale"],
        alternatives=json.loads(row["alternatives"]) if row["alternatives"] else [],
        participants=json.loads(row["participants"]) if row["participants"] else [],
        confirmed_at=row["confirmed_at"],
        created_at=row["created_at"],
        memory_id=row["memory_id"],
        evidence=json.loads(row["evidence"]) if row.get("evidence") else [],
    )
    return out


def brief_lines(decisions: Iterable[Mapping[str, Any]], *, human_names: Mapping[str, str] | None = None) -> list[str]:
    """Data-block lines for in-force decisions: ``D-7 <title> (confirmed by Mani)``.

    Only ``in_force`` rows are rendered, whatever is passed. Titles are agent or
    human text, so each line is clipped and neutralised (``schemas.clip_item``)
    and must be placed inside the ``<remembra-data>`` block by the caller.
    """
    names = human_names or {}
    out = []
    for d in decisions:
        if d.get("state") != "in_force":
            continue
        who = d.get("confirmed_by") or ""
        confirmer = names.get(who, "a human") if who else "a human"
        author = "" if d.get("decided_by_kind") == "human" else f", proposed by {schemas.clip_item(str(d.get('decided_by')), 40)}"
        out.append(schemas.clip_item(f"D-{d.get('number')} {d.get('title')}", 120) + f" (confirmed by {confirmer}{author})")
    return out


class CrewDecisions:
    """Decision service over ``crew_decisions`` bound to the event log, the inbox and the outbox."""

    def __init__(self, log: CrewEventLog, inbox: CrewInbox | None = None, *, limits: CrewLimits | None = None) -> None:
        self.log = log
        self.inbox = inbox or CrewInbox(log)
        self.limits = limits or SELF_HOSTED_CREW_LIMITS

    @property
    def db(self) -> Any:
        return self.log.db

    # -- create --------------------------------------------------------------------------------

    async def create(
        self,
        crew: CrewRef,
        author: Author,
        *,
        title: Any,
        decision: Any,
        rationale: Any = None,
        alternatives: Any = None,
        task_id: str | None = None,
        zone_id: str | None = None,
        source: str = "direct",
        supersedes_id: str | None = None,
        evidence: Any = None,
    ) -> dict[str, Any]:
        """Create ``D-n``: in force for a human author, proposed for an agent. Returns the API view with ``seq``."""
        clean_title = _flat(title, MAX_TITLE, "title")
        clean_decision = _flat(decision, MAX_DECISION, "decision")
        clean_rationale = _flat(rationale, MAX_RATIONALE, "rationale", required=False)
        alts = self._alternatives(alternatives)
        proof = self._evidence(evidence)
        if source not in schemas.DECISION_SOURCES:
            raise ValidationFailed(f"source must be one of {schemas.DECISION_SOURCES}")
        async with self.log.transaction() as tx:
            if task_id is not None:
                await require_same_crew(tx.conn, crew.id, "task", task_id)
            if zone_id is not None:
                await require_same_crew(tx.conn, crew.id, "zone", zone_id)
            row, seq = await self._insert(
                tx,
                crew,
                author,
                title=str(clean_title),
                decision=str(clean_decision),
                rationale=clean_rationale,
                alternatives=alts,
                task_id=task_id,
                zone_id=zone_id,
                source=source,
                supersedes_id=supersedes_id,
                evidence=proof,
            )
        out = decision_api(row)
        out["seq"] = seq
        return out

    async def create_in_tx(
        self, tx: EventTx, crew: CrewRef, author: Author, *, title: str, decision: str
    ) -> tuple[dict[str, Any], int]:
        """Create a decision inside the caller's transaction (a ``kind=decision`` channel message)."""
        clean_title = _flat(title, MAX_TITLE, "title")
        clean_decision = _flat(decision, MAX_DECISION, "decision")
        return await self._insert(
            tx,
            crew,
            author,
            title=str(clean_title),
            decision=str(clean_decision),
            rationale=None,
            alternatives=[],
            task_id=None,
            zone_id=None,
            source="direct",
            supersedes_id=None,
        )

    async def _insert(
        self,
        tx: EventTx,
        crew: CrewRef,
        author: Author,
        *,
        title: str,
        decision: str,
        rationale: str | None,
        alternatives: list[str],
        task_id: str | None,
        zone_id: str | None,
        source: str,
        supersedes_id: str | None,
        evidence: list[str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        store = CrewStore(self.db)
        number = await store.next_number(crew.id, "crew_decisions")
        decision_id = new_id("decision")
        now = now_iso()
        human = author.is_human
        await tx.conn.execute(
            """
            INSERT INTO crew_decisions (id, crew_id, number, title, decision, rationale, alternatives, decided_by_kind,
                decided_by, participants, source, task_id, zone_id, supersedes_id, state, confirmed_by, confirmed_at,
                created_at, proposed_by_verified, decided_by_verified, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                crew.id,
                number,
                title,
                decision,
                rationale,
                json.dumps(alternatives),
                "human" if human else "agent",
                author.principal,
                json.dumps([author.label]),
                source,
                task_id,
                zone_id,
                supersedes_id,
                "in_force" if human else "proposed",
                author.user_id if human else None,
                now if human else None,
                now,
                # Riders (gap analysis §7): whether the proposer's identity was verified (a human
                # login, or a key-verified agent), and the decider's (only a human decides).
                1 if author.verified else 0,
                1 if human else None,
                json.dumps(evidence) if evidence else None,
            ),
        )
        row = await self._get(tx, decision_id)
        assert row is not None
        if human:
            result = await tx.emit(
                crew_id=crew.id,
                type="decision.confirmed",
                actor=author.actor(),
                payload={"decision": decision_view(row)},
                summary=f"D-{number} in force (human)",
                refs={"decision_id": decision_id, "task_id": task_id, "zone_id": zone_id},
            )
            await self._mirror(tx, crew, row)
        else:
            result = await tx.emit(
                crew_id=crew.id,
                type="decision.proposed",
                actor=author.actor(),
                payload={"decision": decision_view(row)},
                summary=f"{author.label} proposed D-{number} (needs confirmation)",
                refs={"decision_id": decision_id, "task_id": task_id, "zone_id": zone_id, "session_id": author.session_id},
            )
            label = author.label
            await self.inbox.raise_item(
                crew_id=crew.id,
                audience="project",
                kind=NEEDS_YOU_KIND,
                origin="agent",
                agent=author.agent_origin(),
                title=lambda n: f"{label} proposed {n} decision{'s' if n != 1 else ''} to confirm",
                ref_type="decision",
                ref_id=decision_id,
                primary_action="confirm_decision",
                actor=author.actor(),
            )
        return row, result.seq

    @staticmethod
    def _alternatives(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_ALTERNATIVES:
            raise ValidationFailed(f"alternatives must be a list of at most {MAX_ALTERNATIVES} strings")
        out = []
        for item in value:
            text = _flat(item, MAX_ALTERNATIVE, "alternative")
            out.append(str(text))
        return out

    @staticmethod
    def _evidence(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_EVIDENCE:
            raise ValidationFailed(f"evidence must be a list of at most {MAX_EVIDENCE} strings")
        return [str(_flat(item, MAX_EVIDENCE_ITEM, "evidence item")) for item in value]

    # -- human decisions -----------------------------------------------------------------------------

    async def confirm(self, decision_id: str, human: Author, crew: CrewRef) -> dict[str, Any]:
        """``proposed → in_force`` (human only); resolves the proposer's Needs-you item when nothing else is pending."""
        self._require_human(human)
        async with self.log.transaction() as tx:
            row = await self._require(tx, decision_id, crew.id)
            if row["state"] == "in_force":
                return {**decision_api(row), "seq": None}
            if row["state"] != "proposed":
                raise DecisionStateConflict(f"D-{row['number']} is {row['state']}")
            now = now_iso()
            await tx.conn.execute(
                "UPDATE crew_decisions SET state = 'in_force', confirmed_by = ?, confirmed_at = ?, decided_by_verified = 1"
                " WHERE id = ? AND state = 'proposed'",
                (human.user_id, now, decision_id),
            )
            row = await self._require(tx, decision_id, crew.id)
            result = await tx.emit(
                crew_id=crew.id,
                type="decision.confirmed",
                actor=human.actor(),
                payload={"decision": decision_view(row)},
                summary=f"D-{row['number']} confirmed by human",
                refs={"decision_id": decision_id, "task_id": row["task_id"], "zone_id": row["zone_id"]},
            )
            await self._mirror(tx, crew, row)
            await self._settle_needs_you(tx, row, human)
        return {**decision_api(row), "seq": result.seq}

    async def reject(self, decision_id: str, human: Author, crew: CrewRef) -> dict[str, Any]:
        """``proposed → rejected`` (human only)."""
        self._require_human(human)
        async with self.log.transaction() as tx:
            row = await self._require(tx, decision_id, crew.id)
            if row["state"] == "rejected":
                return {**decision_api(row), "seq": None}
            if row["state"] != "proposed":
                raise DecisionStateConflict(f"D-{row['number']} is {row['state']}")
            await tx.conn.execute(
                "UPDATE crew_decisions SET state = 'rejected', confirmed_by = ?, confirmed_at = ? WHERE id = ?",
                (human.user_id, now_iso(), decision_id),
            )
            row = await self._require(tx, decision_id, crew.id)
            result = await tx.emit(
                crew_id=crew.id,
                type="decision.rejected",
                actor=human.actor(),
                payload={"decision": decision_view(row)},
                summary=f"D-{row['number']} rejected by human",
                refs={"decision_id": decision_id},
            )
            await self._settle_needs_you(tx, row, human)
        return {**decision_api(row), "seq": result.seq}

    async def supersede(
        self,
        decision_id: str,
        human: Author,
        crew: CrewRef,
        *,
        title: Any,
        decision: Any,
        rationale: Any = None,
    ) -> dict[str, Any]:
        """Replace an in-force decision: the old one becomes ``superseded``, a new in-force ``D-n`` points at it."""
        self._require_human(human)
        clean_title = _flat(title, MAX_TITLE, "title")
        clean_decision = _flat(decision, MAX_DECISION, "decision")
        clean_rationale = _flat(rationale, MAX_RATIONALE, "rationale", required=False)
        async with self.log.transaction() as tx:
            old = await self._require(tx, decision_id, crew.id)
            if old["state"] != "in_force":
                raise DecisionStateConflict(f"only an in-force decision can be superseded (D-{old['number']} is {old['state']})")
            await tx.conn.execute("UPDATE crew_decisions SET state = 'superseded' WHERE id = ?", (decision_id,))
            old = await self._require(tx, decision_id, crew.id)
            await tx.emit(
                crew_id=crew.id,
                type="decision.superseded",
                actor=human.actor(),
                payload={"decision": decision_view(old)},
                summary=f"D-{old['number']} superseded by human",
                refs={"decision_id": decision_id},
            )
            new_row, seq = await self._insert(
                tx,
                crew,
                human,
                title=str(clean_title),
                decision=str(clean_decision),
                rationale=clean_rationale,
                alternatives=[],
                task_id=old["task_id"],
                zone_id=old["zone_id"],
                source="direct",
                supersedes_id=decision_id,
            )
        return {**decision_api(new_row), "seq": seq, "superseded": decision_api(old)}

    # -- reads ---------------------------------------------------------------------------------------

    async def list_decisions(
        self, crew_id: str, *, states: Iterable[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        wanted = list(states or schemas.DECISION_STATES)
        bad = [s for s in wanted if s not in schemas.DECISION_STATES]
        if bad:
            raise ValidationFailed(f"unknown decision state {bad[0]!r}")
        marks = ", ".join("?" for _ in wanted)
        rows = await self.db.fetchall(
            f"SELECT * FROM crew_decisions WHERE crew_id = ? AND state IN ({marks}) ORDER BY number DESC LIMIT ?",  # noqa: S608
            [crew_id, *wanted, max(1, min(int(limit), 500))],
        )
        return [decision_api(r) for r in rows]

    async def in_force(self, crew_id: str) -> list[dict[str, Any]]:
        """Decisions an agent may be told about (the brief's "Decisions in force"): ``in_force`` only."""
        return await self.list_decisions(crew_id, states=["in_force"])

    # -- helpers ------------------------------------------------------------------------------------

    @staticmethod
    def _require_human(author: Author) -> None:
        if not author.is_human:
            raise NotAllowed("Only a human can confirm, reject or supersede a decision.")

    async def _get(self, tx: EventTx, decision_id: str) -> dict[str, Any] | None:
        async with tx.conn.execute("SELECT * FROM crew_decisions WHERE id = ?", (decision_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _require(self, tx: EventTx, decision_id: str, crew_id: str) -> dict[str, Any]:
        row = await self._get(tx, decision_id)
        if row is None or row["crew_id"] != crew_id:
            raise DecisionNotFound("decision not found")
        return row

    async def _settle_needs_you(self, tx: EventTx, row: Mapping[str, Any], human: Author) -> None:
        """Resolve the proposer's coalesced Needs-you item once none of its decisions is still proposed."""
        if row["decided_by_kind"] != "agent":
            return
        async with tx.conn.execute(
            "SELECT COUNT(*) FROM crew_decisions WHERE crew_id = ? AND decided_by = ? AND state = 'proposed'",
            (row["crew_id"], row["decided_by"]),
        ) as cur:
            pending = await cur.fetchone()
        if pending is not None and int(pending[0]) > 0:
            return
        async with tx.conn.execute("SELECT user_id FROM crew_sessions WHERE id = ?", (row["decided_by"],)) as cur:
            sess = await cur.fetchone()
        if sess is None:
            return
        key = agent_dedupe_key(str(sess[0]), str(row["decided_by"]), NEEDS_YOU_KIND)
        async with tx.conn.execute(
            "SELECT id FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
            (row["crew_id"], key),
        ) as cur:
            live = await cur.fetchone()
        if live is not None:
            await self.inbox.resolve(str(live[0]), by=human.user_id, actor=human.actor())

    async def _mirror(self, tx: EventTx, crew: CrewRef, row: Mapping[str, Any]) -> None:
        """Queue the in-force decision as ONE ``decision`` memory (outbox, deduplicated, counted per day)."""
        store = CrewStore(self.db)
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with tx.conn.execute(
            "SELECT COUNT(*) FROM crew_outbox WHERE crew_id = ? AND kind = ? AND created_at >= ?",
            (crew.id, KIND_MEMORY_PROMOTION, now_iso(today)),
        ) as cur:
            count_row = await cur.fetchone()
        verdict = promotion_decision(int(count_row[0]) if count_row else 0, "decision", self.limits)
        lines = [f"Decision D-{row['number']}: {row['title']}", str(row["decision"])]
        if row["rationale"]:
            lines.append(f"Rationale: {row['rationale']}")
        lines.append(
            "Confirmed by a human." if row["decided_by_kind"] == "agent" else "Decided by a human (in force immediately)."
        )
        outbox_id = await store.enqueue_outbox(
            crew.id,
            KIND_MEMORY_PROMOTION,
            {
                "user_id": crew.owner_user_id,
                "project_id": crew.project_id,
                "memory_type": "decision",
                "content": "\n".join(lines),
                "metadata": {
                    "crew_decision_id": row["id"],
                    "decision_ref": f"D-{row['number']}",
                    "confirmed_by": row["confirmed_by"],
                    "decided_by_kind": row["decided_by_kind"],
                },
            },
            dedupe_key=f"decision:{row['id']}",
        )
        if not verdict.promote:
            tomorrow = today + timedelta(days=1)
            await tx.conn.execute(
                "UPDATE crew_outbox SET next_attempt_at = ? WHERE id = ? AND state = 'pending'", (now_iso(tomorrow), outbox_id)
            )
