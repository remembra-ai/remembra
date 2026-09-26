"""An unchanged POST /session/status re-send is not charged against any plan limit.

Agents re-send the same status value on every turn (``deploy:api = green``).
The endpoint already stored nothing for them (``changed: False``), but the plan
gate ran first: the re-send counted toward the Free daily unenriched-write cap
and the per-minute relay burst limit, so a chatty agent could lock itself out
of real writes. The gate now runs only when a new value is stored. The routes,
the plan gate, the metering ledger and the memory store are the production
ones (tests/_cost_harness.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests._cost_harness import cost_app

STATUS = "/api/v1/session/status"


async def _unenriched_today(c, uid: str) -> int:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    cursor = await c.h.db.conn.execute(
        "SELECT COALESCE(SUM(unenriched_writes), 0) FROM cloud_usage_daily WHERE user_id = ? AND date = ?", (uid, today)
    )
    return int((await cursor.fetchone())[0])


async def _status_rows(c, uid: str) -> list[tuple[str, str | None]]:
    cursor = await c.h.db.conn.execute(
        "SELECT content, superseded_by FROM memories WHERE user_id = ? AND memory_type = 'status' ORDER BY created_at",
        (uid,),
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


async def test_unchanged_resend_does_not_use_the_free_daily_cap(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("status-cap@example.com")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await c.h.db.conn.execute(
            "INSERT INTO cloud_usage_daily (user_id, date, unenriched_writes) VALUES (?, ?, 299)", (uid, today)
        )
        await c.h.db.conn.commit()

        r = await c.h.client.post(STATUS, json={"key": "deploy:api", "value": "green"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["changed"] is True
        assert await _unenriched_today(c, uid) == 300  # the last write the Free day allows

        # The same value again, many times: stored nothing, charged nothing.
        for _ in range(5):
            r = await c.h.client.post(STATUS, json={"key": "deploy:api", "value": "green"}, headers=hdr)
            assert r.status_code == 200, r.text
            assert r.json()["changed"] is False
        # Surrounding whitespace is not a change either (the value is stored stripped).
        r = await c.h.client.post(STATUS, json={"key": "deploy:api", "value": "  green \n"}, headers=hdr)
        assert r.status_code == 200 and r.json()["changed"] is False, r.text
        assert await _unenriched_today(c, uid) == 300
        month = await c.meter.get_monthly_usage(uid)
        assert month["relay_events"] == 1

        # A real change is still gated: the day is used up.
        r = await c.h.client.post(STATUS, json={"key": "deploy:api", "value": "red"}, headers=hdr)
        assert r.status_code == 429, r.text
        assert "stores without enrichment per day" in r.json()["detail"]
        # Refused before anything was written: the current value is still "green".
        assert await _status_rows(c, uid) == [("deploy:api: green", None)]


async def test_unchanged_resend_does_not_use_the_relay_burst(tmp_path) -> None:
    async with cost_app(tmp_path, rate_limit_enabled=True) as c:
        uid, hdr = await c.account("status-burst@example.com")
        account = await c.meter.get_account(uid)
        burst = account.limits.relay_burst_per_min
        assert burst == 30  # Free

        r = await c.h.client.post(STATUS, json={"key": "build", "value": "passing"}, headers=hdr)
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        # More unchanged re-sends than the burst allows in a minute.
        codes = [
            (await c.h.client.post(STATUS, json={"key": "build", "value": "passing"}, headers=hdr)).status_code
            for _ in range(burst + 5)
        ]
        assert codes == [200] * (burst + 5)

        # The burst budget is intact: a real change goes through and is recorded.
        r = await c.h.client.post(STATUS, json={"key": "build", "value": "failing: test_billing"}, headers=hdr)
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        rows = await _status_rows(c, uid)
        assert [content for content, _ in rows] == ["build: passing", "build: failing: test_billing"]
        assert rows[0][1] is not None and rows[1][1] is None  # the old value is superseded
        assert (await c.meter.get_monthly_usage(uid))["relay_events"] == 2


async def test_changed_values_still_hit_the_relay_burst(tmp_path) -> None:
    async with cost_app(tmp_path, rate_limit_enabled=True) as c:
        uid, hdr = await c.account("status-burst-changed@example.com")
        burst = (await c.meter.get_account(uid)).limits.relay_burst_per_min
        codes = [
            (await c.h.client.post(STATUS, json={"key": "counter", "value": f"v{i}"}, headers=hdr)).status_code
            for i in range(burst + 1)
        ]
        assert codes[:burst] == [200] * burst
        assert codes[burst] == 429
        # The refused write stored nothing: the current value is the last accepted one.
        r = await c.h.client.get(STATUS, headers=hdr)
        assert r.status_code == 200, r.text
        values = {item["key"]: item["value"] for item in r.json()["items"]}
        assert values == {"counter": f"v{burst - 1}"}


async def test_pii_blocked_status_is_refused_before_the_gate(tmp_path) -> None:
    """Screening now runs first: a refused value no longer spends the day's allowance."""
    async with cost_app(tmp_path) as c:
        from remembra.security.pii_detector import PIIDetector

        c.h.app.state.pii_detector = PIIDetector(mode="block")
        uid, hdr = await c.account("status-pii@example.com")
        r = await c.h.client.post(STATUS, json={"key": "contact", "value": "card 4111 1111 1111 1111 exp 12/29"}, headers=hdr)
        assert r.status_code == 400, r.text
        assert await _unenriched_today(c, uid) == 0
        assert (await c.meter.get_monthly_usage(uid))["relay_events"] == 0
