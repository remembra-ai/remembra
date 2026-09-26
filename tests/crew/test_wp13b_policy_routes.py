"""WP-13b: the two reads the Zone Map and the Policy panel need from the WP-5 routers.

* ``GET /crews/{id}/zones`` also returns the stored folder tree snapshot (``tree``), so the
  Zone Map can overlay zones on the repository's folders (§9.5 "repo tree snapshot").
* ``GET /crews/{id}/bypass-codes`` (human only) lists the crew's bypass codes with a server-computed
  ``state`` (active, used, expired) and never the code itself (D34, §9.1 Policy).

Real app, real auth (JWT vs admin API key), real crew.db, real services.
"""

from __future__ import annotations

from datetime import timedelta

from remembra.crew.store import now_iso
from remembra.crew.zones import utcnow
from tests.crew.test_crew_wp5_routes import CREW, FOREIGN, crew_http

TREE = {
    "name": ".",
    "files": 2,
    "children": [
        {"name": "src", "files": 1, "children": [{"name": "pos", "files": 12}, {"name": "reports", "files": 4}]},
        {"name": "docs", "files": 3},
    ],
}


async def test_zone_listing_carries_the_stored_tree_snapshot(tmp_path):
    async with crew_http(tmp_path) as (h, _db, ctx):
        c = h.client
        base = f"/api/v1/crews/{CREW}"
        before = await c.get(f"{base}/zones", headers=ctx["jwt"])
        assert before.status_code == 200, before.text
        assert before.json()["tree"] is None

        put = await c.put(f"{base}/tree", json={"tree": TREE}, headers=ctx["a"])
        assert put.status_code == 200, put.text

        after = (await c.get(f"{base}/zones", headers=ctx["jwt"])).json()
        assert after["tree"]["tree"] == TREE
        assert after["tree"]["node_count"] == 5
        assert after["tree"]["captured_at"]
        # an agent (API key + session) reads the same listing
        agent = await c.get(f"{base}/zones", headers=ctx["a"])
        assert agent.status_code == 200 and agent.json()["tree"]["tree"] == TREE
        # another tenant's crew stays invisible
        foreign = await c.get(f"/api/v1/crews/{FOREIGN}/zones", headers=ctx["jwt"])
        assert foreign.status_code == 404


async def test_bypass_code_listing_is_human_only_and_never_shows_the_code(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        c = h.client
        base = f"/api/v1/crews/{CREW}"
        empty = await c.get(f"{base}/bypass-codes", headers=ctx["jwt"])
        assert empty.status_code == 200, empty.text
        assert empty.json()["codes"] == [] and empty.json()["server_time"]

        issued = []
        for scope in ("push", "commit", "write:pos"):
            res = await c.post(
                f"{base}/bypass-codes", json={"session_id": "cs_b", "scope": scope, "minutes": 5}, headers=ctx["jwt"]
            )
            assert res.status_code == 201, res.text
            issued.append(res.json())
        # use the push code, expire the commit code
        used = await c.post(
            "/api/v1/bypass-codes/redeem", json={"code": issued[0]["code"], "session_id": "cs_b"}, headers=ctx["b"]
        )
        assert used.status_code == 200, used.text
        async with db.transaction():
            await db.conn.execute(
                "UPDATE crew_bypass_codes SET expires_at = ? WHERE id = ?",
                (now_iso(utcnow() - timedelta(minutes=1)), issued[1]["code_id"]),
            )

        res = await c.get(f"{base}/bypass-codes", headers=ctx["jwt"])
        assert res.status_code == 200
        codes = {row["id"]: row for row in res.json()["codes"]}
        assert codes[issued[0]["code_id"]]["state"] == "used" and codes[issued[0]["code_id"]]["used_at"]
        assert codes[issued[1]["code_id"]]["state"] == "expired"
        assert codes[issued[2]["code_id"]]["state"] == "active"
        assert codes[issued[2]["code_id"]]["scope"] == "write:pos"
        assert all(row["session_id"] == "cs_b" and row["issued_by"] == ctx["owner"] for row in codes.values())
        body = res.text
        for code in issued:
            assert code["code"] not in body  # the plain code is shown once, at issue
        assert "code_hash" not in body

        # an admin API key is never a human principal (D27); agents never see the mechanism (D34)
        for headers in (ctx["a"], {"X-API-Key": ctx["key"]}):
            denied = await c.get(f"{base}/bypass-codes", headers=headers)
            assert denied.status_code == 403, denied.text
        foreign = await c.get(f"/api/v1/crews/{FOREIGN}/bypass-codes", headers=ctx["jwt"])
        assert foreign.status_code == 404
