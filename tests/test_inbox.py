"""Tests for InboxManager — targeted agent-to-agent delivery (issue #9)."""

from datetime import UTC, datetime, timedelta

import pytest

from remembra.inbox.manager import InboxManager, VALID_INBOX_STATUSES, TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def inbox(in_memory_db):
    """InboxManager backed by an in-memory SQLite database."""
    mgr = InboxManager(in_memory_db)
    await mgr.init_schema()
    return mgr


# ---------------------------------------------------------------------------
# Schema & constants
# ---------------------------------------------------------------------------


def test_valid_statuses_are_well_known():
    # "accepted" (taken on, not finished) joined the model with F53
    assert set(VALID_INBOX_STATUSES) == {"unread", "read", "accepted", "done", "blocked", "rejected"}
    assert set(TERMINAL_STATUSES) == {"done", "blocked", "rejected"}
    assert TERMINAL_STATUSES.issubset(VALID_INBOX_STATUSES)


@pytest.mark.asyncio
async def test_init_schema_is_idempotent(in_memory_db):
    mgr = InboxManager(in_memory_db)
    await mgr.init_schema()
    await mgr.init_schema()  # second call must not raise
    # Table must exist and be empty
    cursor = await in_memory_db.conn.execute(
        "SELECT COUNT(*) AS c FROM agent_inbox",
    )
    row = await cursor.fetchone()
    assert row["c"] == 0


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSend:
    async def test_send_basic(self, inbox):
        row = await inbox.send(
            owner_user_id="u1",
            from_agent="agent-a",
            to_agent="agent-b",
            subject="hello",
            body="please run X",
        )
        assert row["inbox_id"].startswith("inbox_")
        assert row["owner_user_id"] == "u1"
        assert row["from_agent"] == "agent-a"
        assert row["to_agent"] == "agent-b"
        assert row["subject"] == "hello"
        assert row["body"] == "please run X"
        assert row["status"] == "unread"
        assert row["metadata"] == {}
        assert row["created_at"]
        assert row["ack_at"] is None

    async def test_send_with_metadata_and_expiry(self, inbox):
        meta = {"priority": "high", "tags": ["urgent"]}
        expiry = datetime.now(UTC) + timedelta(hours=1)
        row = await inbox.send(
            owner_user_id="u1",
            from_agent="a",
            to_agent="b",
            subject="s",
            body="b",
            metadata=meta,
            expires_at=expiry,
        )
        assert row["metadata"] == meta
        assert row["expires_at"] is not None

    async def test_send_rejects_empty_fields(self, inbox):
        with pytest.raises(ValueError, match="from_agent"):
            await inbox.send(owner_user_id="u1", from_agent="", to_agent="b", subject="s", body="b")
        with pytest.raises(ValueError, match="to_agent"):
            await inbox.send(owner_user_id="u1", from_agent="a", to_agent="", subject="s", body="b")
        with pytest.raises(ValueError, match="subject"):
            await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="", body="b")
        with pytest.raises(ValueError, match="body"):
            await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="  ")

    async def test_send_strips_whitespace(self, inbox):
        row = await inbox.send(
            owner_user_id="u1",
            from_agent="  a  ",
            to_agent="  b  ",
            subject="  s  ",
            body="body",
        )
        assert row["from_agent"] == "a"
        assert row["to_agent"] == "b"
        assert row["subject"] == "s"


# ---------------------------------------------------------------------------
# Get for agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetForAgent:
    async def test_unread_by_default(self, inbox):
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s1", body="1")
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s2", body="2")
        rows = await inbox.get_for_agent("u1", "b")
        assert len(rows) == 2
        assert {r["subject"] for r in rows} == {"s1", "s2"}
        assert all(r["status"] == "unread" for r in rows)

    async def test_scoped_by_to_agent(self, inbox):
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="for-b", body="x")
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="c", subject="for-c", body="x")
        rows = await inbox.get_for_agent("u1", "b")
        assert len(rows) == 1
        assert rows[0]["subject"] == "for-b"

    async def test_scoped_by_owner(self, inbox):
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="u1", body="x")
        await inbox.send(owner_user_id="u2", from_agent="a", to_agent="b", subject="u2", body="x")
        u1_rows = await inbox.get_for_agent("u1", "b")
        u2_rows = await inbox.get_for_agent("u2", "b")
        assert len(u1_rows) == 1 and u1_rows[0]["subject"] == "u1"
        assert len(u2_rows) == 1 and u2_rows[0]["subject"] == "u2"

    async def test_expired_rows_filtered(self, inbox):
        past = datetime.now(UTC) - timedelta(hours=1)
        future = datetime.now(UTC) + timedelta(hours=1)
        await inbox.send(
            owner_user_id="u1",
            from_agent="a",
            to_agent="b",
            subject="expired",
            body="x",
            expires_at=past,
        )
        await inbox.send(
            owner_user_id="u1",
            from_agent="a",
            to_agent="b",
            subject="live",
            body="x",
            expires_at=future,
        )
        rows = await inbox.get_for_agent("u1", "b")
        assert len(rows) == 1
        assert rows[0]["subject"] == "live"

    async def test_all_returns_acked_rows_too(self, inbox):
        r1 = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="1", body="x")
        await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="2", body="x")
        # Ack the first one
        await inbox.ack("u1", r1["inbox_id"], result="done")

        unread = await inbox.get_for_agent("u1", "b", status="unread")
        all_rows = await inbox.get_for_agent("u1", "b", status="all")
        assert len(unread) == 1
        assert unread[0]["subject"] == "2"
        assert len(all_rows) == 2

    async def test_limit(self, inbox):
        for i in range(5):
            await inbox.send(
                owner_user_id="u1",
                from_agent="a",
                to_agent="b",
                subject=f"s{i}",
                body="x",
            )
        rows = await inbox.get_for_agent("u1", "b", limit=3)
        assert len(rows) == 3

    async def test_bad_status_raises(self, inbox):
        with pytest.raises(ValueError, match="status"):
            await inbox.get_for_agent("u1", "b", status="nonsense")

    async def test_empty_agent_raises(self, inbox):
        with pytest.raises(ValueError, match="agent_id"):
            await inbox.get_for_agent("u1", "   ")


# ---------------------------------------------------------------------------
# Ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAck:
    async def test_ack_read_without_result(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        updated = await inbox.ack("u1", r["inbox_id"])
        assert updated["status"] == "read"
        assert updated["ack_at"] is not None
        assert updated["ack_result"] is None

    async def test_ack_with_terminal_result(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        updated = await inbox.ack("u1", r["inbox_id"], result="done", note="shipped it")
        assert updated["status"] == "done"
        assert updated["ack_result"] == "done"
        assert updated["ack_note"] == "shipped it"

    async def test_ack_blocked(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        updated = await inbox.ack("u1", r["inbox_id"], result="blocked", note="missing cred")
        assert updated["status"] == "blocked"
        assert updated["ack_result"] == "blocked"

    async def test_ack_rejected(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        updated = await inbox.ack("u1", r["inbox_id"], result="rejected")
        assert updated["status"] == "rejected"
        assert updated["ack_result"] == "rejected"

    async def test_ack_invalid_result_raises(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        with pytest.raises(ValueError, match="result"):
            await inbox.ack("u1", r["inbox_id"], result="maybe")

    async def test_ack_missing_raises(self, inbox):
        with pytest.raises(ValueError, match="not found"):
            await inbox.ack("u1", "inbox_doesnotexist")

    async def test_ack_wrong_owner_raises(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        # u2 cannot ack u1's row
        with pytest.raises(ValueError, match="not found"):
            await inbox.ack("u2", r["inbox_id"])

    async def test_get_one_respects_owner(self, inbox):
        r = await inbox.send(owner_user_id="u1", from_agent="a", to_agent="b", subject="s", body="x")
        assert await inbox.get_one("u1", r["inbox_id"]) is not None
        assert await inbox.get_one("u2", r["inbox_id"]) is None


# ---------------------------------------------------------------------------
# Round-trip (DoD from issue #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_agent_a_to_agent_b(inbox):
    """Agent A sends a directive to test-agent-b, B picks it up, acts, acks."""
    # A sends
    sent = await inbox.send(
        owner_user_id="mani",
        from_agent="agent-a",
        to_agent="test-agent-b",
        subject="run dummy task",
        body="Please respond with 'ok' in a memory.",
        metadata={"kind": "directive"},
    )
    assert sent["status"] == "unread"

    # B boots, reads inbox
    inbox_rows = await inbox.get_for_agent("mani", "test-agent-b")
    assert len(inbox_rows) == 1
    item = inbox_rows[0]
    assert item["subject"] == "run dummy task"
    assert item["body"] == "Please respond with 'ok' in a memory."
    assert item["metadata"]["kind"] == "directive"
    assert item["inbox_id"] == sent["inbox_id"]

    # B acks as done
    acked = await inbox.ack("mani", item["inbox_id"], result="done", note="task complete")
    assert acked["status"] == "done"
    assert acked["ack_result"] == "done"
    assert acked["ack_note"] == "task complete"

    # B's inbox is now empty (unread-only view)
    empty = await inbox.get_for_agent("mani", "test-agent-b")
    assert empty == []

    # But it shows up in 'all'
    all_rows = await inbox.get_for_agent("mani", "test-agent-b", status="all")
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "done"
