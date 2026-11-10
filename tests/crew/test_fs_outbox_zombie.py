"""F42: the crew outbox does not bring back a record that was delivered and removed while it was being flushed.

``flush`` reads every record first, then awaits the sends one by one. A session's ``end()`` that finished in
the meantime had already removed its ``leave-<key>`` record and forgotten the session; the sender then
answered "retry" for the stale copy and ``flush`` wrote the file back. That zombie was retried every five
minutes for about 42 hours with ``last_error`` null, held every later ``leave`` record behind it, and would
end the same agent session again if it was resumed.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from remembra.relay.crew import outbox as O
from remembra.relay.crew.gate import Layout
from tests.crew.wp9_support import crew_server, make_repo, new_crewd, peer


async def test_a_record_removed_while_an_earlier_one_was_being_sent_is_not_sent_or_written_back(tmp_path):
    box = tmp_path / "outbox"
    O.spool(box, "leave", {"op": "end", "args": {"reason": "other"}}, session_key="a", name="leave-a", now=100.0)
    second = O.spool(box, "leave", {"op": "end", "args": {"reason": "other"}}, session_key="b", name="leave-b", now=101.0)
    seen: list[str] = []

    async def send(e: O.Entry) -> str:
        seen.append(str(e.session_key))
        if e.session_key == "a":
            second.unlink()  # b's own end() finished while a's replay was awaited: delivered, record removed
            return "done"
        return "retry"  # what crewd answers for a leave whose session it no longer holds

    res = await O.flush(box, send, now=200.0, stop_on_retry=False)
    assert seen == ["a"], "the stale copy of a removed record must not be replayed"
    assert not second.exists() and O.read_entries(box) == []
    assert res.sent == 1 and res.retried == 0 and res.dropped == 1


async def test_a_record_removed_during_its_own_send_is_not_written_back(tmp_path):
    """A replayed leave the server answers with an error: end() has removed the record and ended the session,
    yet reports a failure, and the sender says "retry"."""
    box = tmp_path / "outbox"
    path = O.spool(box, "leave", {"op": "end", "args": {"reason": "other"}}, session_key="k", name="leave-k", now=100.0)

    async def send(e: O.Entry) -> str:
        path.unlink()
        return "retry"

    res = await O.flush(box, send, now=200.0)
    assert not path.exists() and O.read_entries(box) == []
    assert res.retried == 0 and res.dropped == 1
    # and nothing comes back on later passes
    res = await O.flush(box, send, now=10_000.0)
    assert res == O.FlushResult()


async def test_a_record_replaced_during_its_send_keeps_its_attempts_and_backoff(tmp_path):
    """An outage: end() spools the same-name record again (new idem) and the server is unreachable. The retry
    must still count, or the record would be replayed on every tick for ever."""
    box = tmp_path / "outbox"
    path = O.spool(box, "leave", {"op": "end", "args": {"reason": "other"}}, session_key="k", name="leave-k", now=100.0)

    async def send(e: O.Entry) -> str:
        O.spool(box, "leave", {"op": "end", "args": {"reason": "other"}}, session_key="k", name="leave-k", now=200.0)
        return "retry"

    res = await O.flush(box, send, now=200.0)
    [left] = O.read_entries(box)
    assert res.retried == 1 and left.attempts == 1 and left.next_attempt_at > 200.0 and left.path == path
    res = await O.flush(box, send, now=201.0)
    assert res.retried == 0 and res.skipped == 1  # in backoff


async def test_a_plain_retry_leaves_a_reason_on_the_record(tmp_path):
    box = tmp_path / "outbox"
    path = O.spool(box, "leave", {"op": "end", "args": {}}, session_key="k", name="leave-k", now=100.0)

    async def send(e: O.Entry) -> str:
        return "retry"

    await O.flush(box, send, now=200.0)
    assert json.loads(path.read_text())["last_error"], "a record that keeps retrying must say something about why"

    async def boom(e: O.Entry) -> str:
        raise RuntimeError("x")

    await O.flush(box, boom, now=10_000.0)
    assert json.loads(path.read_text())["last_error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# The same race through the real crewd: three sessions end while the outbox is being flushed.
# ---------------------------------------------------------------------------


class _SlowLeave(httpx.AsyncBaseTransport):
    """The crew server, with each ``/leave`` answered after the next delay in ``delays``."""

    def __init__(self, inner: httpx.AsyncBaseTransport, delays: list[float]) -> None:
        self.inner, self.delays = inner, list(delays)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leave") and self.delays:
            await asyncio.sleep(self.delays.pop(0))
        return await self.inner.handle_async_request(request)


async def test_sessions_ending_during_a_flush_leave_no_zombie_leave_record(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    pids = (41001, 41002, 41003)
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive=set(pids), transport=_SlowLeave(srv.transport(), [0.5, 0.9, 0.2]))
        joined = []
        for n, pid in enumerate(pids):
            args = {
                "adapter": "claude-code",
                "agent_id": "claude-code",
                "client_session_id": f"sess-{n}",
                "cwd": str(repo),
                "agent_pid": pid,
                "source": "startup",
            }
            joined.append(await d.join(peer(pid), args))
        ends = [
            asyncio.create_task(d.end(peer(pid), {"key": j["key"], "reason": "other"}, grace=False))
            for pid, j in zip(pids, joined, strict=True)
        ]
        await asyncio.sleep(0.1)  # all three have spooled their leave record and are waiting for the server
        assert len(O.read_entries(layout.outbox, ("leave",))) == 3
        await d.flush_outbox()  # replays the first two (each waits for its own end), then reaches the third
        results = await asyncio.gather(*ends)
        assert all(r["ok"] for r in results), results
        rows = await srv.rows("SELECT state FROM crew_sessions ORDER BY id")
        assert [r["state"] for r in rows] == ["ended"] * 3
        assert d.sessions == {}
        assert O.read_entries(layout.outbox, ("leave",)) == [], "a leave record came back for a session that has ended"
        again = await d.flush_outbox()
        assert again.retried == 0
