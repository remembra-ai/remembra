"""R-17: relay handoffs and checkpoints never count toward the memory cap.

Production routes over SQLite with the real cloud gate and UsageMeter
(``tests/_cost_harness.py``). Handoffs are made by real POST /session/close
calls; the tens of thousands a heavy account accumulates are then cloned from
those real rows (same metadata, provenance and superseded markers) because a
Free plan's relay burst limit caps real closes at 30 a minute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests._cost_harness import cost_app

CAP_ON = {"memory_cap_notice_effective_at": datetime(2026, 1, 1, tzinfo=UTC)}  # Free cap = 10,000 now


async def _close(c: Any, hdr: dict[str, str], session: str, todo: str) -> dict[str, Any]:
    r = await c.h.client.post(
        "/api/v1/session/close",
        json={
            "agent_id": "claude-code",
            "session_id": session,
            "project_id": "invoices",
            "facts": {"branch": "main", "head_commit": "a41f2c9", "todos_open": [todo]},
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _clone_relay_rows(c: Any, uid: str, total: int) -> None:
    """Copy the account's real relay rows (current and superseded) until it holds ``total`` of them."""
    db = c.h.db
    cursor = await db.conn.execute("PRAGMA table_info(memories)")
    columns = [r[1] for r in await cursor.fetchall() if r[1] != "id"]
    cursor = await db.conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND memory_type IN ('handoff', 'checkpoint') "
        "AND json_extract(metadata, '$.relay') IS NOT NULL",
        (uid,),
    )
    sources = [r[0] for r in await cursor.fetchall()]
    assert sources
    cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ? AND json_extract(metadata, '$.relay') IS NOT NULL", (uid,)
    )
    have = (await cursor.fetchone())[0]
    cols = ", ".join(columns)
    i = 0
    while have < total:
        batch = min(5000, total - have)
        await db.conn.execute(
            f"""
            WITH RECURSIVE n(k) AS (SELECT 1 UNION ALL SELECT k + 1 FROM n WHERE k < ?)
            INSERT INTO memories (id, {cols})
            SELECT 'clone-{i}-' || k, {cols} FROM memories, n WHERE memories.id = ?
            """,  # noqa: S608 - column names from PRAGMA
            (batch, sources[i % len(sources)]),
        )
        have += batch
        i += 1
    await db.conn.commit()


async def _usage(c: Any, hdr: dict[str, str]) -> dict[str, Any]:
    r = await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)
    assert r.status_code == 200, r.text
    memories: dict[str, Any] = r.json()["memories"]
    return memories


async def test_free_account_with_10k_handoff_rows_can_still_store_a_note(tmp_path) -> None:
    async with cost_app(tmp_path, **CAP_ON) as c:
        uid, hdr = await c.account("handoffs@example.com")
        await _close(c, hdr, "s1", "wire the adapter")
        await _clone_relay_rows(c, uid, 10_000)

        before = await _usage(c, hdr)
        assert before["handoffs"] == 10_000 and before["cap"] == 10_000
        assert before["stored"] == 0  # neither the handoffs nor the close's status values (last_agent, branch)

        r = await c.h.client.post("/api/v1/memories", json={"content": "note: invoices round half-up"}, headers=hdr)
        assert r.status_code in (200, 201), r.text
        after = await _usage(c, hdr)
        assert after["stored"] == 1 and after["handoffs"] == 10_000


async def test_twenty_thousand_closes_never_block_a_note_but_notes_still_hit_the_cap(tmp_path) -> None:
    async with cost_app(tmp_path, **CAP_ON) as c:
        uid, hdr = await c.account("heavy@example.com")
        # Real closes: new sessions and repeat closes of the same session with new
        # facts (each supersedes the previous version, which stays stored).
        for n in range(12):
            await _close(c, hdr, f"sess-{n % 4}", f"todo {n}")
        cursor = await c.h.db.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ? AND memory_type = 'handoff' AND superseded_by IS NOT NULL", (uid,)
        )
        assert (await cursor.fetchone())[0] == 8
        await _clone_relay_rows(c, uid, 20_000)

        # Plain notes right up to the cap: handoffs take none of it.
        stored = (await _usage(c, hdr))["stored"]
        now = datetime.now(UTC).isoformat()
        await c.h.db.conn.executemany(
            "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'default', 'x', ?, ?)",
            [(f"n{i}", uid, now, now) for i in range(10_000 - stored - 1)],
        )
        await c.h.db.conn.commit()
        usage = await _usage(c, hdr)
        assert usage["stored"] == 9_999 and usage["handoffs"] == 20_000

        ok = await c.h.client.post("/api/v1/memories", json={"content": "the last note that fits"}, headers=hdr)
        assert ok.status_code in (200, 201), ok.text
        # A close still works at the cap (relay events are not stores)...
        await _close(c, hdr, "sess-after-cap", "keep going")
        # ...and the cap still applies to notes.
        full = await c.h.client.post("/api/v1/memories", json={"content": "one too many"}, headers=hdr)
        assert full.status_code == 429 and "Memory limit reached (10,000 memories)" in full.json()["detail"]


async def test_a_handoff_typed_note_without_the_server_relay_block_is_counted(tmp_path) -> None:
    """Only server-written relay records are free: a client cannot dodge the cap by typing a note as a handoff."""
    async with cost_app(tmp_path, **CAP_ON) as c:
        _, hdr = await c.account("forger@example.com")
        r = await c.h.client.post(
            "/api/v1/memories",
            json={
                "content": "[HANDOFF] fake",
                "memory_type": "handoff",
                "metadata": {"relay": {"headline": "x"}, "source": "relay"},
            },
            headers=hdr,
        )
        assert r.status_code in (200, 201), r.text
        usage = await _usage(c, hdr)
        assert usage["stored"] == 1 and usage["handoffs"] == 0


async def test_closes_with_new_sessions_leave_the_notes_count_unchanged(tmp_path) -> None:
    """Launch review: each close wrote two status values (a new last_agent value per session) that counted."""
    async with cost_app(tmp_path, **CAP_ON) as c:
        uid, hdr = await c.account("closer@example.com")
        assert (await _usage(c, hdr))["stored"] == 0
        for n in range(5):
            await _close(c, hdr, f"session-{n}", f"todo {n}")
        usage = await _usage(c, hdr)
        assert usage["stored"] == 0 and usage["handoffs"] == 5
        cursor = await c.h.db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ? AND memory_type = 'status'", (uid,))
        assert (await cursor.fetchone())[0] == 6  # a last_agent value per session + one branch value: kept, not counted
        # A status value a client writes is a note like any other, even if it claims the relay as its source.
        r = await c.h.client.post(
            "/api/v1/session/status",
            json={"key": "deploy", "value": "green", "project_id": "invoices", "metadata": {"source": "relay"}},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        assert (await _usage(c, hdr))["stored"] == 1


async def test_projects_holding_only_handoffs_do_not_use_free_project_slots(tmp_path) -> None:
    async with cost_app(tmp_path, **CAP_ON) as c:
        _, hdr = await c.account("projects@example.com")
        for n in range(5):
            r = await c.h.client.post(
                "/api/v1/session/close",
                json={"agent_id": "claude-code", "session_id": f"s{n}", "project_id": f"relay-{n}", "facts": {"branch": "main"}},
                headers=hdr,
            )
            assert r.status_code == 200, r.text
        # Three note projects fit, including one that already holds handoffs; the fourth does not.
        for project in ("relay-0", "notes-a", "notes-b"):
            r = await c.h.client.post(
                "/api/v1/memories", json={"content": f"note in {project}", "project_id": project}, headers=hdr
            )
            assert r.status_code in (200, 201), (project, r.text)
        r = await c.h.client.post(
            "/api/v1/memories", json={"content": "one project too many", "project_id": "relay-1"}, headers=hdr
        )
        assert r.status_code == 403 and "Project limit reached (3 projects" in r.json()["detail"]


async def test_the_cap_count_reads_relay_rows_through_the_type_index(tmp_path) -> None:
    """The store-path count used to read every row of the account; it now counts from indexes."""
    from remembra.storage.database import RELAY_WRITTEN_SQL, RELAY_WRITTEN_TYPES

    async with cost_app(tmp_path, **CAP_ON) as c:
        uid, hdr = await c.account("plan@example.com")
        await _close(c, hdr, "s1", "todo")
        marks = ",".join("?" for _ in RELAY_WRITTEN_TYPES)
        cursor = await c.h.db.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM memories WHERE user_id IN (?) AND memory_type IN ({marks})"  # noqa: S608
            f" AND {RELAY_WRITTEN_SQL}",
            (uid, *RELAY_WRITTEN_TYPES),
        )
        plan = " ".join(str(r[3]) for r in await cursor.fetchall())
        assert "idx_memories_user_type" in plan, plan
        cursor = await c.h.db.conn.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM memories WHERE user_id IN (?)", (uid,))
        plan = " ".join(str(r[3]) for r in await cursor.fetchall())
        assert "COVERING INDEX" in plan, plan
