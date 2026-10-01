"""Pruned-range adjacency uses the existing ordered primary key."""

import pytest

from remembra.crew.retention import _record_pruned_runs
from tests.crew.wp5_support import CREW, open_db, seed_crew


async def test_pruned_range_predecessor_lookup_does_not_scan_all_prior_gaps(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        async with db.transaction():
            await db.conn.executemany(
                "INSERT INTO crew_pruned_ranges(crew_id,first_seq,last_seq,prev_hash,last_hash,pruned_at)"
                " VALUES(?,?,?,'before','after','synthetic')",
                [(CREW, i * 3 + 1, i * 3 + 1) for i in range(2000)],
            )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        async with db.transaction():
            await db.conn.set_progress_handler(progress, 100)
            try:
                await _record_pruned_runs(db.conn, CREW, [(6000, 6000, "new-before", "new-after")])
            finally:
                await db.conn.set_progress_handler(None, 0)
        assert steps < 500, f"Adjacency probe scanned historical gaps: {steps} SQLite VM operations"
        assert await db.fetchone(
            "SELECT last_seq,prev_hash,last_hash FROM crew_pruned_ranges WHERE crew_id=? AND first_seq=6000", (CREW,)
        ) == {"last_seq": 6000, "prev_hash": "new-before", "last_hash": "new-after"}
    finally:
        await db.close()


@pytest.mark.parametrize("left,right", [(False, False), (True, False), (False, True), (True, True)])
async def test_pruned_range_merge_preserves_exact_outer_chain_links(tmp_path, left, right):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        async with db.transaction():
            if left:
                await _record_pruned_runs(db.conn, CREW, [(1, 5, "left-before", "left-after")])
            if right:
                await _record_pruned_runs(db.conn, CREW, [(9, 12, "right-before", "right-after")])
            await _record_pruned_runs(db.conn, CREW, [(6, 8, "middle-before", "middle-after")])
        assert await db.fetchall(
            "SELECT first_seq,last_seq,prev_hash,last_hash FROM crew_pruned_ranges WHERE crew_id=?", (CREW,)
        ) == [
            {
                "first_seq": 1 if left else 6,
                "last_seq": 12 if right else 8,
                "prev_hash": "left-before" if left else "middle-before",
                "last_hash": "right-after" if right else "middle-after",
            }
        ]
    finally:
        await db.close()
