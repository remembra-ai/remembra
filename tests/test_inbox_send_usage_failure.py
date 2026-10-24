"""
A relay-usage metering failure after an inbox send has committed must not turn
the stored send into an error: the sender would resend and store it twice.
"""

import os

# Disable auth + rate limiting — MUST precede remembra imports
os.environ["REMEMBRA_AUTH_ENABLED"] = "false"
os.environ["REMEMBRA_RATE_LIMIT_ENABLED"] = "false"

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

import remembra.api.v1.inbox as inbox_api
from remembra.inbox.manager import InboxManager
from remembra.main import app


class _FailingMeter:
    def __init__(self) -> None:
        self.calls = 0

    async def record_relay_event(self, user_id: str, count: int) -> None:
        self.calls += 1
        raise RuntimeError("meter down")


@pytest.fixture()
async def setup(monkeypatch: pytest.MonkeyPatch):
    class _DB:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self.conn = conn

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    mgr = InboxManager(_DB(conn))
    await mgr.init_schema()
    meter = _FailingMeter()

    async def _no_guard(*args: object, **kwargs: object) -> None:
        return None

    # The burst/soft-cap guard reads plan state from the meter; this test is about the record step only.
    monkeypatch.setattr(inbox_api, "relay_guard", _no_guard)
    monkeypatch.setattr(app.state, "inbox_manager", mgr, raising=False)
    monkeypatch.setattr(app.state, "usage_meter", meter, raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, conn, meter

    await conn.close()


async def test_send_succeeds_once_when_usage_metering_fails(setup) -> None:
    client, conn, meter = setup
    r = await client.post(
        "/api/v1/inbox/send",
        json={"to_agent": "agent-b", "from_agent": "agent-a", "subject": "s", "body": "b"},
    )
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    assert meter.calls == 1

    cursor = await conn.execute("SELECT inbox_id FROM agent_inbox")
    rows = await cursor.fetchall()
    assert [row["inbox_id"] for row in rows] == [inbox_id]
