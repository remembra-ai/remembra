"""Persistent open work: omission from a newer handoff is never resolution.

Content stays in the existing protected memory store. The ledger holds only
references, state and provenance. Resolution proposed by an agent remains in
the open view until an authenticated account holder accepts its evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from remembra.storage.continuity_schema import item_digest
from remembra.storage.database import Database


def reported_open_work(facts: dict[str, Any]) -> dict[str, list[str]]:
    """Full bounded input lists, independently of the handoff's display caps."""
    from remembra.relay.handoff import build_sections, latest_tests

    todos = [f"TODO: {t}" for t in (facts.get("todos_open") or [])[:100] if str(t).strip()]
    errors = [f"error: {e}" for e in (facts.get("errors") or [])[:100] if str(e).strip()]
    tests = latest_tests((facts.get("tests") or [])[:100])
    failing = [line for test in tests for line in build_sections({"tests": [test]})["failing"]]
    test_cmds = {t.get("cmd") for t in tests}
    commands = {c.get("cmd"): c for c in (facts.get("commands") or [])[:200]}
    failing += [
        line for cmd, c in commands.items() if cmd not in test_cmds for line in build_sections({"commands": [c]})["failing"]
    ]
    return {"not_done": list(dict.fromkeys(todos)), "failing": list(dict.fromkeys(failing + errors))}


class ContinuityConflict(ValueError):
    """State changed or the supplied evidence reference is not accessible."""


class ContinuityService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def capture(
        self, *, user_id: str, project_id: str, handoff_id: str, sections: dict[str, Any], closed_at: str, agent_id: str
    ) -> None:
        """Capture only explicit TODOs/failures, not generic missing git facts.

        Re-closes and older offline deliveries do not undo an accepted
        resolution. A genuinely newer report of the same problem reopens it.
        """
        entries = [("todo", i, s) for i, s in enumerate(sections.get("not_done") or []) if str(s).startswith("TODO: ")]
        entries += [("failure", i, s) for i, s in enumerate(sections.get("failing") or [])]
        async with self.db.transaction():
            for kind, index, text in entries:
                digest = item_digest(user_id, project_id, kind, text)
                rows = list(await self.db.conn.execute_fetchall("SELECT * FROM continuity_items WHERE id = ?", (digest,)))
                old = dict(rows[0]) if rows else None
                if old and (old["source_handoff_id"] == handoff_id or old["last_seen_at"] >= closed_at):
                    continue
                version = old["version"] + 1 if old else 1
                await self.db.conn.execute(
                    """INSERT INTO continuity_items
                       (id,user_id,project_id,kind,state,source_handoff_id,section_index,first_seen_at,last_seen_at,version)
                       VALUES (?,?,?,?,'open',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       state='open',source_handoff_id=excluded.source_handoff_id,section_index=excluded.section_index,
                       last_seen_at=excluded.last_seen_at,resolution_memory_id=NULL,resolution_digest=NULL,
                       confirmed_by=NULL,version=excluded.version""",
                    (digest, user_id, project_id, kind, handoff_id, index, closed_at, closed_at, version),
                )
                await self._event(digest, user_id, project_id, "open", handoff_id, agent_id, "agent", version)

    async def _event(
        self,
        item_id: str,
        user: str,
        project: str,
        state: str,
        memory: str | None,
        actor: str,
        kind: str,
        version: int,
        digest: str | None = None,
    ) -> None:
        await self.db.conn.execute(
            """INSERT INTO continuity_events
               (item_id,user_id,project_id,state,source_memory_id,actor_id,actor_kind,created_at,version,evidence_digest)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item_id, user, project, state, memory, actor, kind, datetime.now(UTC).isoformat(), version, digest),
        )

    async def transition(
        self,
        *,
        user_id: str,
        project_id: str,
        item_id: str,
        version: int,
        action: str,
        actor_id: str,
        human: bool,
        evidence_memory_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare-and-swap, with fresh reference and state checks in one transaction."""
        if action not in {"propose_resolution", "confirm_resolution", "reopen"}:
            raise ValueError("Unknown continuity action")
        if action == "confirm_resolution" and not human:
            raise PermissionError("Only an authenticated account holder can confirm a resolution")
        async with self.db.transaction():
            rows = list(
                await self.db.conn.execute_fetchall(
                    "SELECT * FROM continuity_items WHERE id = ? AND user_id = ? AND project_id = ?",
                    (item_id, user_id, project_id),
                )
            )
            if not rows:
                raise KeyError("Open-work item not found")
            row = dict(rows[0])
            if row["version"] != version:
                raise ContinuityConflict("Item changed; read the current version before acting")
            if action == "confirm_resolution":
                if row["state"] != "resolution_proposed":
                    raise ContinuityConflict("No resolution is proposed")
                evidence_memory_id = row["resolution_memory_id"]
            digest = None
            if action != "reopen":
                evidence = list(
                    await self.db.conn.execute_fetchall(
                        """SELECT content,metadata FROM memories WHERE id = ? AND user_id = ? AND project_id = ?
                       AND (expires_at IS NULL OR julianday(expires_at) > julianday('now'))""",
                        (evidence_memory_id, user_id, project_id),
                    )
                )
                if not evidence:
                    raise ContinuityConflict("Accessible, unexpired evidence memory is required")
                digest = hashlib.sha256(json.dumps(list(evidence[0]), ensure_ascii=False).encode()).hexdigest()
                if action == "confirm_resolution" and digest != row["resolution_digest"]:
                    raise ContinuityConflict("Evidence changed after the proposal; submit a new proposal for review")
            state = {"propose_resolution": "resolution_proposed", "confirm_resolution": "resolved", "reopen": "open"}[action]
            if action == "reopen":
                evidence_memory_id = None
            await self.db.conn.execute(
                """UPDATE continuity_items SET state=?,resolution_memory_id=?,resolution_digest=?,
                   confirmed_by=?,version=version+1 WHERE id=?""",
                (state, evidence_memory_id, digest, user_id if state == "resolved" else None, item_id),
            )
            await self._event(
                item_id,
                user_id,
                project_id,
                state,
                evidence_memory_id,
                actor_id,
                "human" if human else "agent",
                version + 1,
                digest,
            )
            return {"id": item_id, "state": state, "version": version + 1, "evidence_memory_id": evidence_memory_id}

    async def open_work(self, user_id: str, project_id: str, *, limit: int = 10, after: str = "") -> dict[str, Any]:
        """A bounded page plus total; recent handoffs do not select this view."""
        limit = min(max(limit, 1), 100)
        rows = list(
            await self.db.conn.execute_fetchall(
                """SELECT c.*,m.metadata,m.expires_at,m.trust_score FROM continuity_items c
               JOIN memories m ON m.id=c.source_handoff_id
               WHERE c.user_id=? AND c.project_id=? AND c.state!='resolved' AND c.id>? ORDER BY c.id LIMIT ?""",
                (user_id, project_id, after, limit + 1),
            )
        )
        total = list(
            await self.db.conn.execute_fetchall(
                "SELECT COUNT(*) FROM continuity_items WHERE user_id=? AND project_id=? AND state!='resolved'",
                (user_id, project_id),
            )
        )
        items = []
        for raw in list(rows)[:limit]:
            row = dict(raw)
            try:
                metadata = json.loads(row.pop("metadata") or "{}")
            except (ValueError, TypeError):
                metadata = {}
            relay = metadata.get("relay") if isinstance(metadata, dict) else None
            section = "not_done" if row["kind"] == "todo" else "failing"
            report = relay.get("open_work", relay) if isinstance(relay, dict) else {}
            values = report.get(section) if isinstance(report, dict) else None
            values = values if isinstance(values, list) else []
            expired = row.pop("expires_at")
            unavailable = False
            if expired:
                try:
                    expiry = datetime.fromisoformat(expired.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=UTC)
                    unavailable = expiry <= datetime.now(UTC)
                except (ValueError, TypeError):
                    unavailable = True
            row["text"] = (
                "Source evidence expired or unavailable"
                if unavailable or row["section_index"] >= len(values)
                else values[row["section_index"]]
            )
            row["facts_source"] = "agent-declared"
            row["evidence_available"] = not unavailable and row["section_index"] < len(values)
            items.append(row)
        return {"total": total[0][0], "items": items, "next_after": items[-1]["id"] if len(rows) > limit and items else None}
