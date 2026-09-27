"""WP-13b scene: a crew in the states the Zone Map and the Policy panel must show, built through the
real WP-5 services (zones.yml uploads, tree snapshot, claims, the no-zone bootstrap) on a real
crew.db, with events on the real bus so a connected dashboard follows it live.

Used by the live test (``test_wp13b_dashboard_live.py``) and by the local preview server
(``wp13b_preview_server.py``). Every credential here is a test value for a throwaway local server.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi import FastAPI

import remembra.config as config_module
from remembra.auth.users import UserManager
from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.limits import crew_limits_for_tier
from tests.crew import wp8_seed as seed
from tests.crew.wp5_support import seed_session

EMAIL = "mani@example.com"
PASSWORD = "Str0ng!Passw0rd"  # test value for the throwaway local server

TREE: dict[str, Any] = {
    "name": ".",
    "files": 6,
    "children": [
        {
            "name": "src",
            "files": 2,
            "children": [
                {
                    "name": "app",
                    "files": 3,
                    "children": [
                        {"name": "pos", "files": 14},
                        {"name": "invoices", "files": 9},
                        {"name": "reports", "files": 6},
                        {"name": "settings", "files": 4},
                    ],
                },
                {"name": "billing", "files": 7},
                {"name": "payroll", "files": 5},
                {"name": "lib", "files": 11},
            ],
        },
        {"name": "supabase", "files": 1, "children": [{"name": "migrations", "files": 23}]},
        {"name": "docs", "files": 8},
    ],
}

ZONES_V1 = """\
version: 1
zones:
  pos:
    title: POS section
    include: [src/app/pos/**]
  invoices:
    title: Invoices and GCT
    include: [src/app/invoices/**]
  reports:
    title: Reports
    include: [src/app/reports/**]
    mode: shared
  billing:
    title: Billing
    include: [src/billing/**]
  payroll:
    title: Payroll
    include: [src/payroll/**]
    protected: true
commons:
  package.json: plain
ignore: [docs/**]
"""

# cs_b (an agent) removes payroll: loosening, so it waits for a human (D9)
ZONES_V2 = ZONES_V1.replace(
    """  payroll:
    title: Payroll
    include: [src/payroll/**]
    protected: true
""",
    "",
)

POSAPP_TREE: dict[str, Any] = {
    "name": ".",
    "files": 3,
    "children": [
        {
            "name": "src",
            "files": 1,
            "children": [{"name": "checkout", "files": 8}, {"name": "catalog", "files": 5}, {"name": "cart", "files": 4}],
        },
        {"name": "docs", "files": 2},
    ],
}


def stale_token(user_id: str, email: str = EMAIL, minutes_ago: int = 20) -> str:
    """A valid login that is too old for step-up actions (§5.9)."""
    issued = int(time.time() * 1000) - minutes_ago * 60_000
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "iat": issued // 1000,
            "iat_ms": issued,
            "exp": int(time.time()) + 3600,
            "type": "access",
        },
        config_module.get_settings().jwt_secret,
        algorithm="HS256",
    )


async def _zone_id(app: FastAPI, crew_id: str, slug: str) -> str:
    row = await app.state.crew_db.fetchone("SELECT id FROM crew_zones WHERE crew_id = ? AND slug = ?", (crew_id, slug))
    assert row is not None, slug
    return str(row["id"])


async def build(app: FastAPI) -> dict[str, Any]:
    users: UserManager = app.state.users
    user, error = await users.create_user(email=EMAIL, password=PASSWORD)
    assert user is not None, error
    token = users.create_jwt_token(user.id, EMAIL)
    db = app.state.crew_db
    ops = Z.CrewOps(app.state.crew_events, None, crew_limits_for_tier("pro"))

    # -- yaadbooks: repo zones, live claims, a queue, a pending loosening change -------------------------
    crew_id = await seed.crew(db, user.id, "yaadbooks")
    a, _ = await seed_session(db, "cs_a", crew_id=crew_id, user_id=user.id, callsign="cc-1", worktree_id="wt-a")
    b, _ = await seed_session(
        db,
        "cs_b",
        crew_id=crew_id,
        user_id=user.id,
        callsign="codex-1",
        agent_id="codex",
        worktree_id="wt-b",
        adapter_enforcement="advisory",
    )
    c, _ = await seed_session(db, "cs_c", crew_id=crew_id, user_id=user.id, callsign="cc-2", worktree_id="wt-b")
    async with db.transaction():
        await db.conn.execute(
            "UPDATE crew_sessions SET branch = 'main', githook_state = 'ok', host_id = 'hst_mbp' WHERE crew_id = ?", (crew_id,)
        )
        await db.conn.execute(
            "UPDATE crew_sessions SET githook_state = 'missing', branch = 'feat/receipts' WHERE id IN ('cs_b', 'cs_c')"
        )
        await db.conn.execute("UPDATE crew_sessions SET adapter = 'codex' WHERE id = 'cs_b'")
    pa, pb, pc = (Z.Principal.for_session(s) for s in (a, b, c))
    # zones.yml saved by a human first (`remembra-crew zones push` at a TTY), then the tree snapshot
    first = await Z.upload_zones_file(
        ops, crew_id, Z.Principal.human(user.id, privileged=True), yaml_text=ZONES_V1, sha="7f3a9c21d0e4", branch="main"
    )
    assert first["result"] == "applied", first
    await Z.put_tree(ops, crew_id, pa, TREE)
    pos = await _zone_id(app, crew_id, "pos")
    reports = await _zone_id(app, crew_id, "reports")
    invoices = await _zone_id(app, crew_id, "invoices")
    got = await C.request_claim(ops, crew_id, pa, zone_id=pos, mode="exclusive", source="mcp")
    assert got.status == "granted", got.body()
    shared = await C.request_claim(ops, crew_id, pc, zone_id=reports, mode="shared", source="mcp")
    assert shared.status == "granted", shared.body()
    queued = await C.request_claim(ops, crew_id, pc, zone_id=pos, mode="exclusive", source="mcp", wait=True)
    assert queued.status == "queued", queued.body()
    inv = await C.request_claim(ops, crew_id, pb, zone_id=invoices, mode="exclusive", source="mcp")
    assert inv.status == "granted", inv.body()
    change = await Z.upload_zones_file(ops, crew_id, pb, yaml_text=ZONES_V2, sha="c41d02be9a77", branch="main")
    assert change["result"] == "pending", change

    # -- posapp: no zones file; the second agent joining bootstraps temporary zones (D37) -----------
    crew2 = await seed.crew(db, user.id, "posapp")
    d, _ = await seed_session(db, "cs_d", crew_id=crew2, user_id=user.id, callsign="cc-1", worktree_id="wt-d")
    await seed_session(db, "cs_e", crew_id=crew2, user_id=user.id, callsign="cc-2", worktree_id="wt-e")
    boot = await Z.put_tree(ops, crew2, Z.Principal.for_session(d), POSAPP_TREE)
    assert len(boot["bootstrap_zone_ids"]) == 3, boot

    return {
        "token": token,
        "stale": stale_token(user.id),
        "user_id": user.id,
        "crew_id": crew_id,
        "crew2": crew2,
        "change_id": change["change_id"],
    }
