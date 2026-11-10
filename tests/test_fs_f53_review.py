"""F53 review follow-up: ``accepted`` never reopens a finished inbox row.

``ack(result="accepted")`` on a row that was already done, blocked or rejected set it back to
``accepted``, so a finished message came back into ``status=open`` and the brief. A finished row now
keeps its status and result; the note is still taken. Real routes, real keys, a real SQLite inbox.
"""

from __future__ import annotations

import pytest

import tests.test_fs_f53_inbox_protocol as f53
from remembra.inbox import manager as inbox_manager
from remembra.inbox.manager import InboxManager
from tests.test_fs_f53_inbox_protocol import _ack, _inbox, _key, _send

h = f53.h


@pytest.mark.parametrize("finished", ["done", "blocked", "rejected"])
async def test_accepting_a_finished_row_does_not_reopen_it(h, finished):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    inbox_id = (await _send(h, codex)).json()["inbox_id"]
    first = await _ack(h, claude, inbox_id, result=finished, note="closed")
    assert first.status_code == 200 and first.json()["status"] == finished, first.text
    again = await _ack(h, claude, inbox_id, result="accepted")
    assert again.status_code == 200, again.text
    assert again.json()["status"] == finished and again.json()["ack_result"] == finished, again.text
    assert again.json()["ack_note"] == "closed"  # no note given: the first one stands
    assert await _inbox(h, claude, "claude-code", "open") == []
    assert await _inbox(h, claude, "claude-code", "accepted") == []
    (row,) = await _inbox(h, claude, "claude-code", "all")
    assert (row["status"], row["ack_result"]) == (finished, finished)
    # a note sent with it is kept, the status still stands
    noted = await _ack(h, claude, inbox_id, result="accepted", note="looked again")
    assert (noted.json()["status"], noted.json()["ack_result"], noted.json()["ack_note"]) == (finished, finished, "looked again")


async def test_a_finished_row_can_still_change_its_terminal_result_and_an_open_one_be_accepted(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    inbox_id = (await _send(h, codex)).json()["inbox_id"]
    assert (await _ack(h, claude, inbox_id)).json()["status"] == "read"
    assert (await _ack(h, claude, inbox_id, result="accepted")).json()["status"] == "accepted"  # read -> accepted
    assert (await _ack(h, claude, inbox_id, result="blocked")).json()["status"] == "blocked"
    assert (await _ack(h, claude, inbox_id, result="done")).json()["status"] == "done"  # blocked -> done, as before


async def test_the_manager_keeps_a_finished_row_when_asked_to_accept_it(h):
    manager: InboxManager = h.created["inbox"]
    sent = await manager.send(owner_user_id="u1", from_agent="codex", to_agent="claude-code", subject="s", body="b")
    await manager.ack("u1", sent["inbox_id"], result="rejected", note="not ours")
    row = await manager.ack("u1", sent["inbox_id"], result="accepted")
    assert (row["status"], row["ack_result"], row["ack_note"]) == ("rejected", "rejected", "not ours")


def test_the_open_filter_names_exactly_the_open_statuses():
    assert inbox_manager._OPEN_SQL == "status IN ('unread', 'read', 'accepted')"
    assert all(f"'{s}'" in inbox_manager._OPEN_SQL for s in inbox_manager.OPEN_STATUSES)
