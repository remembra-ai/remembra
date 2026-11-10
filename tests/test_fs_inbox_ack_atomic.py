"""F53 review: "accepted never reopens a finished row" holds when two acks interleave.

``ack`` read the row and then issued an UPDATE that did not look at the status again. An
``accepted`` (or a plain "mark read") that had read the row while it was still open, and wrote
after a concurrent ``done``, put the finished row back to ``accepted`` / ``read`` and wiped the
finisher's note and result. The status change is now one conditional UPDATE. Real manager, real
SQLite.
"""

from __future__ import annotations

import asyncio

import pytest

from remembra.inbox.manager import TERMINAL_STATUSES, InboxManager

FINISHED = sorted(TERMINAL_STATUSES)


@pytest.fixture()
async def inbox(in_memory_db):
    mgr = InboxManager(in_memory_db)
    await mgr.init_schema()
    return mgr


async def _send(inbox: InboxManager, n: int = 0) -> str:
    row = await inbox.send(owner_user_id="u1", from_agent="codex", to_agent="claude-code", subject=f"s{n}", body="b")
    return str(row["inbox_id"])


def _interleave(inbox: InboxManager, monkeypatch, inbox_id: str, finished: str) -> None:
    """The next ``ack`` reads the row; before it writes, another ack finishes the row (with a note)."""
    real = inbox.get_one
    state = {"armed": True}

    async def get_one(owner, iid, **kw):
        row = await real(owner, iid, **kw)
        if state["armed"] and iid == inbox_id:
            state["armed"] = False
            done = await inbox.ack(owner, iid, result=finished, note="finished by the other seat")
            assert done["status"] == finished
        return row  # stale: read before the other ack finished the row

    monkeypatch.setattr(inbox, "get_one", get_one)


@pytest.mark.parametrize("finished", FINISHED)
@pytest.mark.parametrize("late", ["accepted", None], ids=["accepted", "mark-read"])
@pytest.mark.parametrize("start", ["unread", "read", "accepted"])
@pytest.mark.parametrize("note", [None, "looked at it"], ids=["no-note", "note"])
async def test_an_ack_that_read_an_open_row_never_reopens_it_after_a_concurrent_finish(
    inbox, monkeypatch, finished, late, start, note
):
    inbox_id = await _send(inbox)
    if start == "read":
        await inbox.ack("u1", inbox_id)
    elif start == "accepted":
        await inbox.ack("u1", inbox_id, result="accepted")
    assert (await inbox.get_one("u1", inbox_id))["status"] == start
    _interleave(inbox, monkeypatch, inbox_id, finished)
    row = await inbox.ack("u1", inbox_id, result=late, note=note)
    assert (row["status"], row["ack_result"]) == (finished, finished)
    # the finisher's note stands unless this ack brought one of its own (the rule for a finished row)
    assert row["ack_note"] == (note or "finished by the other seat")
    assert await inbox.get_for_agent("u1", "claude-code", status="open") == []
    assert await inbox.get_for_agent("u1", "claude-code", status="accepted") == []


@pytest.mark.parametrize("finished", FINISHED)
@pytest.mark.parametrize("order", ["finish-first", "accept-first"])
async def test_concurrent_accept_and_finish_leave_every_row_finished(inbox, finished, order):
    ids = [await _send(inbox, n) for n in range(25)]

    async def pair(inbox_id: str):
        fin = inbox.ack("u1", inbox_id, result=finished, note="closed")
        acc = inbox.ack("u1", inbox_id, result="accepted")
        return await asyncio.gather(*((fin, acc) if order == "finish-first" else (acc, fin)))

    await asyncio.gather(*(pair(i) for i in ids))
    rows = await inbox.get_for_agent("u1", "claude-code", status="all", limit=100)
    assert len(rows) == 25
    assert {(r["status"], r["ack_result"], r["ack_note"]) for r in rows} == {(finished, finished, "closed")}
    assert await inbox.get_for_agent("u1", "claude-code", status="open") == []


async def test_the_sequential_transitions_are_unchanged(inbox):
    inbox_id = await _send(inbox)
    assert (await inbox.ack("u1", inbox_id, note="seen"))["status"] == "read"
    row = await inbox.ack("u1", inbox_id)  # a second plain ack of a read row: still read, the write is the same as before
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("read", None, None)
    row = await inbox.ack("u1", inbox_id, result="accepted", note="mine")
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("accepted", "accepted", "mine")
    row = await inbox.ack("u1", inbox_id, result="accepted")  # accepted again: allowed, as before
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("accepted", "accepted", None)
    row = await inbox.ack("u1", inbox_id)  # plain ack of an accepted row: status and note stand
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("accepted", "accepted", None)
    row = await inbox.ack("u1", inbox_id, result="blocked", note="waiting")
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("blocked", "blocked", "waiting")
    row = await inbox.ack("u1", inbox_id, result="accepted")
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("blocked", "blocked", "waiting")
    row = await inbox.ack("u1", inbox_id, result="done", note="shipped")  # a terminal result still replaces another
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("done", "done", "shipped")
    row = await inbox.ack("u1", inbox_id, note="p.s.")
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("done", "done", "p.s.")
    with pytest.raises(ValueError, match="not found"):
        await inbox.ack("u1", "inbox_0000000000", result="accepted")
    with pytest.raises(ValueError, match="result must be one of"):
        await inbox.ack("u1", inbox_id, result="read")
