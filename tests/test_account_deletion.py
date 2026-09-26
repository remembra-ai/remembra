"""R-11 / R-23: self-serve account deletion, end to end.

Real routes and managers over SQLite, a real in-process Qdrant, and the Paddle
API faked at the HTTP layer (tests/_paddle_mock.py), so the real Paddle client
code runs. Covered:

* deleting cancels every billable Paddle subscription (the held one and a
  duplicate) immediately, the resulting ``subscription.canceled`` webhooks are
  applied idempotently, and a failed cancel deletes nothing (502);
* a Google/GitHub sign-in account (no known password) deletes with an emailed
  code; wrong, expired and over-tried codes are refused;
* after the grace period the erasure job leaves no row or vector of the account
  (memory, handoff, inbox item, project link, OAuth grant, webhook, team, a
  crew-style database) and keeps everyone else's;
* the superadmin hard delete runs the same billing cancel and full erasure, and
  re-activating a deleted account inside the grace period undoes the deletion.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from remembra.account.erasure import AccountEraser, ExtraDatabase, TableRule, erasure_digest
from remembra.api.v1 import admin
from remembra.cloud.email import EmailResult, ResendBackend
from remembra.config import Settings
from remembra.connector.store import ConnectorStore
from remembra.storage.database import Database
from remembra.storage.qdrant import QdrantStore
from remembra.teams.manager import TeamManager
from remembra.webhooks.manager import WebhookManager
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests._ingest_fakes import DIM
from tests.connector_harness import pkce
from tests.security_harness import secure_app
from tests.test_account_erasure import init_every_schema, table_columns
from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase
from tests.test_plans_billing_signup import PRICES, _paddle
from tests.test_social_login import ROUTERS as SOCIAL_ROUTERS
from tests.test_social_login import exchange, oauth_settings, providers, sign_in  # noqa: F401 (fixture)

PASSWORD = "Str0ng!Passw0rd"

# A crew-style second database and the explicit rules it must come with: the
# account's own crews go with every child row; in anyone else's crew only its
# own rows go (membership, messages and their edit history), and rows it merely
# acted on (added a member, created a task) stay with the actor cleared.
_OWN_CREWS = "SELECT id FROM crews WHERE owner_user_id = :uid"
_OWN_MESSAGES = "SELECT id FROM crew_messages WHERE author_user_id = :uid"
CREW_TOY_RULES = (
    TableRule(
        "crew_message_edits",
        deletes=(f"crew_id IN ({_OWN_CREWS})", f"message_id IN ({_OWN_MESSAGES})"),
    ),
    TableRule("crew_messages", deletes=(f"crew_id IN ({_OWN_CREWS})", "author_user_id = :uid")),
    TableRule("crew_tasks", deletes=(f"crew_id IN ({_OWN_CREWS})",), nulls=(("created_by", "created_by = :uid"),)),
    TableRule(
        "crew_members",
        deletes=(f"crew_id IN ({_OWN_CREWS})", "user_id = :uid"),
        nulls=(("added_by", "added_by = :uid"),),
    ),
    TableRule("crews", deletes=("owner_user_id = :uid",)),
)


class Outbox:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> EmailResult:  # installed on the class as a bound method
        self.sent.append(message)
        return EmailResult(success=True, message_id=f"msg_{len(self.sent)}")

    def last_code(self) -> str:
        match = re.search(r">(\d{6})<", self.sent[-1].html)
        assert match, self.sent[-1].html
        return match.group(1)


@pytest.fixture()
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(ResendBackend, "send", box.send)
    return box


async def _qdrant() -> QdrantStore:
    store = QdrantStore(Settings(openai_api_key="t", embedding_dimensions=DIM, qdrant_collection="deletion_test"))
    store._client = AsyncQdrantClient(location=":memory:")
    await store.init_collection(DIM)
    return store


async def _points(store: QdrantStore, user_id: str) -> int:
    from qdrant_client.http import models as qm

    client = await store._get_client()
    result = await client.count(
        collection_name=store.collection_name,
        count_filter=qm.Filter(must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))]),
    )
    return int(result.count)


async def _cells(conn: Any, needles: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for table, columns in (await table_columns(conn)).items():
        if table.startswith("memories_fts_") or table == "sqlite_sequence":
            continue
        where = " OR ".join(f'CAST("{c}" AS TEXT) = ?' for c, _t, _n in columns for _ in needles)
        params = [n for _c in columns for n in needles]
        cursor = await conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params)
        n = int((await cursor.fetchone())[0])
        if n:
            found[table] = n
    return found


async def _fill_account(c: Any, uid: str, hdr: dict[str, str], tag: str) -> None:
    """A memory, a relay handoff, an inbox item and a project link, through the API."""
    r = await c.h.client.post(
        "/api/v1/memories", json={"content": f"{tag} prefers dark roast", "project_id": "widget"}, headers=hdr
    )
    assert r.status_code == 201, r.text
    r = await c.h.client.post(
        "/api/v1/session/close",
        json={"agent_id": "claude-code", "session_id": f"s-{tag}", "project_id": "widget", "facts": {}},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    r = await c.h.client.post(
        "/api/v1/inbox/send", json={"to_agent": "codex", "subject": "pickup", "body": f"review {tag}"}, headers=hdr
    )
    assert r.status_code in (200, 201), r.text
    r = await c.h.client.post(
        "/api/v1/projects/links", json={"from_project": "widget", "to_project": "gizmo", "relation": "related"}, headers=hdr
    )
    assert r.status_code == 200, r.text


async def _connect_oauth(db: Any, uid: str) -> None:
    store = ConnectorStore(db, rotation_key=b"r" * 32)
    await store.init_schema()
    client, _secret = await store.register_client(
        client_name="Claude",
        redirect_uris=["https://claude.ai/cb"],
        grant_types=["authorization_code"],
        token_endpoint_auth_method="none",
    )
    verifier, challenge = pkce()
    code = await store.create_grant_with_code(
        user_id=uid,
        client_id=client["client_id"],
        scopes=["memory:recall"],
        resource="https://api.example.com/mcp",
        project_ids=[],
        agent_id="claude-ai",
        redirect_uri="https://claude.ai/cb",
        code_challenge=challenge,
        authenticated_at=store.now(),
    )
    await store.exchange_code(
        code=code, client_id=client["client_id"], redirect_uri="https://claude.ai/cb", code_verifier=verifier
    )


async def _grants(db: Any, uid: str) -> list[str]:
    cursor = await db.conn.execute("SELECT grant_id FROM oauth_grants WHERE user_id = ?", (uid,))
    return [str(r[0]) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Password account with billing: cancel, then erase everything
# ---------------------------------------------------------------------------


async def test_deletion_cancels_billing_and_the_job_erases_every_row_and_vector(tmp_path, monkeypatch) -> None:
    from remembra.webhooks import manager as webhook_manager

    async def public_dns(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(webhook_manager, "_resolve", public_dns)
    paddle = _paddle_mock.install(monkeypatch)
    alerts = RecordingAlerts()
    crew_db = Database(str(tmp_path / "crew.db"))
    await crew_db.connect()
    await crew_db.conn.executescript(
        "CREATE TABLE crews (id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, project_id TEXT);"
        "CREATE TABLE crew_members (crew_id TEXT, user_id TEXT, added_by TEXT, PRIMARY KEY (crew_id, user_id));"
        "CREATE TABLE crew_messages (id TEXT PRIMARY KEY, crew_id TEXT, author_user_id TEXT, body TEXT);"
        "CREATE TABLE crew_message_edits (message_id TEXT, crew_id TEXT, prev_body TEXT);"
        "CREATE TABLE crew_tasks (id TEXT PRIMARY KEY, crew_id TEXT, title TEXT, created_by TEXT);"
    )
    store = await _qdrant()
    try:
        async with cost_app(tmp_path) as c:
            _paddle(c, **PRICES)
            c.h.app.state.alerts = alerts
            c.h.app.state.tasks = None
            c.service.qdrant = store
            c.h.app.state.qdrant = store
            db = c.h.db
            await init_every_schema(db)
            c.h.app.state.account_eraser = AccountEraser(
                db, store, extra_databases=[ExtraDatabase("crew", crew_db, rules=CREW_TOY_RULES)]
            )

            victim, vkey = await c.account("victim@example.com")
            bystander, bkey = await c.account("bystander@example.com")
            vjwt = c.h.jwt(victim, "victim@example.com")

            # A paid Solo subscription, plus a duplicate the customer also pays for.
            await _hook(c, "transaction.completed", _purchase("txn_v", "sub_v", "pri_solo_m", _bound(victim), customer="ctm_v"))
            paddle.add_subscription("sub_v", "ctm_v")
            paddle.add_subscription("sub_dup", "ctm_v")
            paddle.add_subscription("sub_old", "ctm_v", status="canceled")
            paddle.add_subscription("sub_by", "ctm_by")
            assert (await c.meter.get_tenant(victim))["plan"] == "solo"

            for uid, hdr, tag in ((victim, vkey, "victim"), (bystander, bkey, "bystander")):
                await _fill_account(c, uid, hdr, tag)
                await _connect_oauth(db, uid)
                webhooks = WebhookManager(db)
                await webhooks.register(uid, f"https://hooks.example/{tag}", ["memory.stored"])
                await crew_db.conn.execute("INSERT INTO crews VALUES (?, ?, 'widget')", (f"crew_{tag}", uid))
                await crew_db.conn.execute("INSERT INTO crew_members VALUES (?, ?, ?)", (f"crew_{tag}", uid, uid))
                await crew_db.conn.execute(
                    "INSERT INTO crew_messages VALUES (?, ?, ?, 'hello')", (f"msg_{tag}", f"crew_{tag}", uid)
                )
                await crew_db.conn.execute(
                    "INSERT INTO crew_message_edits VALUES (?, ?, 'first draft')", (f"msg_{tag}", f"crew_{tag}")
                )
            # In the bystander's crew the victim added a member, created a task and
            # wrote (then edited) a message; a bystander message in the victim's crew.
            await crew_db.conn.execute("INSERT INTO crew_members VALUES ('crew_bystander', 'u_mate', ?)", (victim,))
            await crew_db.conn.execute("INSERT INTO crew_tasks VALUES ('task_b', 'crew_bystander', 'ship it', ?)", (victim,))
            await crew_db.conn.execute(
                "INSERT INTO crew_messages VALUES ('msg_vb', 'crew_bystander', ?, 'victim words')", (victim,)
            )
            await crew_db.conn.execute("INSERT INTO crew_message_edits VALUES ('msg_vb', 'crew_bystander', 'victim draft')")
            await crew_db.conn.execute(
                "INSERT INTO crew_messages VALUES ('msg_bv', 'crew_victim', ?, 'hi in your crew')", (bystander,)
            )
            await crew_db.conn.commit()
            teams = TeamManager(db)
            team = await teams.create_team("Victim team", owner_id=victim)
            await teams.add_member(team["id"], bystander, invited_by=victim)
            assert await _points(store, victim) > 0 and await _points(store, bystander) > 0

            victim_grants = await _grants(db, victim)
            assert len(victim_grants) == 1

            # --- Delete (password) ------------------------------------------------
            # The victim owns a team with another member: refused until the end of the team is confirmed,
            # before the password is checked or billing touched.
            r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": "wrong"}, headers=vjwt)
            assert r.status_code == 409, r.text
            detail = r.json()["detail"]
            assert detail["code"] == "TEAM_OWNER" and detail["teams"] == [{"id": team["id"], "name": "Victim team", "members": 1}]
            assert "Victim team (1 other member)" in detail["message"]
            assert paddle.requests("POST", "/subscriptions/") == []
            r = await c.h.client.request(
                "DELETE", "/api/v1/auth/me", json={"password": PASSWORD, "end_teams": True}, headers=vjwt
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["subscriptions_cancelled"] == 2
            assert "subscription is cancelled" in body["message"]
            deleted_at = datetime.fromisoformat(body["deleted_at"])
            assert datetime.fromisoformat(body["erasure_after"]) == deleted_at + timedelta(days=7)
            cancels = [call[1] for call in paddle.requests("POST", "/subscriptions/")]
            assert sorted(cancels) == ["/subscriptions/sub_dup/cancel", "/subscriptions/sub_v/cancel"]
            assert paddle.subscriptions["sub_by"]["status"] == "active"  # someone else's billing untouched
            assert (await c.meter.get_tenant(victim))["plan"] == "free"
            # Access ends at once: API key and session both refused.
            assert (await c.h.client.get("/api/v1/memories", headers=vkey)).status_code == 401
            assert (await c.h.client.get("/api/v1/auth/me", headers=vjwt)).status_code == 401

            # Paddle's own webhooks for the cancels arrive (and are retried): idempotent.
            for _ in range(2):
                result = await _hook(c, "subscription.canceled", {"id": "sub_v", "customer_id": "ctm_v", "status": "canceled"})
                assert result["applied"] == "applied"
                assert (await c.meter.get_tenant(victim))["plan"] == "free"
            dup = await _hook(c, "subscription.canceled", {"id": "sub_dup", "customer_id": "ctm_v", "status": "canceled"})
            assert dup["applied"] == "no_change"

            # --- Inside the grace period nothing is erased yet --------------------
            eraser = c.h.app.state.account_eraser
            assert await eraser.erase_due(timedelta(days=7), now=deleted_at + timedelta(days=6)) == []
            assert (await _cells(db.conn, [victim]))["memories"] >= 2

            # --- After it, everything goes ----------------------------------------
            receipts = await eraser.erase_due(timedelta(days=7), now=deleted_at + timedelta(days=7, minutes=1))
            assert [r.digest for r in receipts] == [erasure_digest(victim)]
            assert await _cells(db.conn, [victim, "victim@example.com"]) == {}
            assert await _cells(crew_db.conn, [victim]) == {}
            cursor = await crew_db.conn.execute("SELECT COUNT(*) FROM crew_message_edits WHERE prev_body LIKE 'victim%'")
            assert (await cursor.fetchone())[0] == 0  # edit history keyed by message id only
            assert await _points(store, victim) == 0
            # Everyone else keeps everything.
            kept = await _cells(db.conn, [bystander])
            for table in ("users", "memories", "agent_inbox", "project_links", "oauth_grants", "webhooks"):
                assert kept.get(table, 0) >= 1, table
            # OAuth tokens hang off grants: the victim's are gone, the bystander's stay.
            for grant_ids, expected in ((victim_grants, 0), (await _grants(db, bystander), 2)):
                marks = ",".join("?" for _ in grant_ids)
                cursor = await db.conn.execute(f"SELECT COUNT(*) FROM oauth_tokens WHERE grant_id IN ({marks})", grant_ids)
                assert (await cursor.fetchone())[0] == expected
            assert "team_members" not in kept  # the victim's team, and the bystander's seat in it, are gone
            assert await _points(store, bystander) > 0
            assert await _cells(crew_db.conn, [bystander]) == {"crew_members": 1, "crews": 1, "crew_messages": 1}
            # The bystander's crew keeps the member and the task the victim added, actor cleared.
            cursor = await crew_db.conn.execute("SELECT user_id, added_by FROM crew_members WHERE user_id = 'u_mate'")
            assert [tuple(r) for r in await cursor.fetchall()] == [("u_mate", None)]
            cursor = await crew_db.conn.execute("SELECT id, created_by FROM crew_tasks")
            assert [tuple(r) for r in await cursor.fetchall()] == [("task_b", None)]
            cursor = await crew_db.conn.execute("SELECT message_id FROM crew_message_edits")
            assert [r[0] for r in await cursor.fetchall()] == ["msg_bystander"]
            rows = receipts[0].rows
            assert rows["crew:crew_messages"] == 3 and rows["crew:crew_message_edits"] == 2
            assert receipts[0].unregistered_tables == []

            # A late Paddle event for the erased subscription matches nothing and changes nothing.
            late = await _hook(c, "subscription.canceled", {"id": "sub_v", "customer_id": "ctm_v", "status": "canceled"})
            assert late["applied"] == "unmatched"
            assert not [a for a in alerts.sent if a[0].startswith("paddle_unmatched_purchase")]
    finally:
        await store.close()
        await crew_db.close()


async def test_a_failed_billing_cancel_deletes_nothing(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid, key = await c.account("payer@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_p", "sub_p", "pri_solo_m", _bound(uid), customer="ctm_p"))
        paddle.add_subscription("sub_p", "ctm_p")
        jwt = c.h.jwt(uid, "payer@example.com")

        for failure in (500, httpx.ConnectError("paddle down")):
            paddle.failures["POST /subscriptions/sub_p/cancel"] = failure
            r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=jwt)
            assert r.status_code == 502, r.text
            assert "not deleted" in r.json()["detail"]
            user = await c.h.db.get_user_by_id(uid)
            assert user["is_active"] and user["deleted_at"] is None
            assert (await c.meter.get_tenant(uid))["plan"] == "solo"
            assert (await c.h.client.get("/api/v1/memories", headers=key)).status_code == 200
        paddle.failures["GET /subscriptions"] = 503  # listing fails too: still nothing deleted
        paddle.failures.pop("POST /subscriptions/sub_p/cancel")
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=jwt)
        assert r.status_code == 502 and paddle.subscriptions["sub_p"]["status"] == "active"

        # Paddle answers again. A retry after a cancel that went through but timed out is a success.
        paddle.failures.clear()
        paddle.subscriptions["sub_p"]["status"] = "canceled"
        tries = len(paddle.requests("POST", "/subscriptions/sub_p/cancel"))
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=jwt)
        assert r.status_code == 200, r.text
        assert r.json()["subscriptions_cancelled"] == 1
        assert len(paddle.requests("POST", "/subscriptions/sub_p/cancel")) == tries  # read as cancelled, not re-sent


async def test_cancel_refused_because_already_cancelled_counts_as_done(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid, _key = await c.account("race@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_r", "sub_r", "pri_solo_m", _bound(uid), customer="ctm_r"))
        paddle.add_subscription("sub_r", "ctm_r")
        original = paddle.handler

        def cancel_races_the_webhook(request: httpx.Request) -> httpx.Response:
            # The subscription gets cancelled elsewhere between our status read and our cancel.
            if request.method == "POST" and request.url.path.endswith("/cancel"):
                paddle.subscriptions["sub_r"]["status"] = "canceled"
            return original(request)

        monkeypatch.setattr(paddle, "handler", cancel_races_the_webhook)
        _paddle_mock.install(monkeypatch, paddle)
        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(uid, "race@example.com")
        )
        assert r.status_code == 200, r.text
        assert r.json()["subscriptions_cancelled"] == 1


async def test_free_account_and_wrong_or_missing_confirmation(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid, _key = await c.account("free@example.com")
        jwt = c.h.jwt(uid, "free@example.com")
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": "wrong"}, headers=jwt)
        assert r.status_code == 400 and r.json()["detail"] == "Password is incorrect"
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={}, headers=jwt)
        assert r.status_code == 400 and "emailed code" in r.json()["detail"]
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"code": "123456"}, headers=jwt)
        assert r.status_code == 400 and "wrong or expired" in r.json()["detail"]
        r = await c.h.client.request("DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=jwt)
        assert r.status_code == 200 and r.json()["subscriptions_cancelled"] == 0
        assert "subscription" not in r.json()["message"]
        assert paddle.calls == []  # no billing to cancel, Paddle never called


# ---------------------------------------------------------------------------
# Google / GitHub accounts confirm with an emailed code
# ---------------------------------------------------------------------------


async def test_github_account_deletes_with_an_emailed_code(tmp_path, providers, outbox) -> None:  # noqa: F811
    async with secure_app(tmp_path, SOCIAL_ROUTERS, settings=oauth_settings(resend_api_key="re_test_key")) as h:
        frag = await sign_in(h, providers, "github", from_page="signup")
        session = (await exchange(h, frag["code"])).json()
        hdr = {"Authorization": f"Bearer {session['access_token']}"}
        uid = session["user"]["id"]

        # No password anyone knows: the password path cannot confirm.
        r = await h.client.request("DELETE", "/api/v1/auth/me", json={"password": ""}, headers=hdr)
        assert r.status_code == 400

        r = await h.client.post("/api/v1/auth/me/deletion-code", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["expires_in_minutes"] == 15
        assert outbox.sent[-1].to == "octo@private.example"
        first = outbox.last_code()

        # A new request replaces the old code.
        await h.client.post("/api/v1/auth/me/deletion-code", headers=hdr)
        code = outbox.last_code()
        if code != first:
            r = await h.client.request("DELETE", "/api/v1/auth/me", json={"code": first}, headers=hdr)
            assert r.status_code == 400

        r = await h.client.request("DELETE", "/api/v1/auth/me", json={"code": code}, headers=hdr)
        assert r.status_code == 200, r.text
        user = await h.db.get_user_by_id(uid)
        assert not user["is_active"] and user["deleted_at"]
        # Single use.
        assert (await h.client.get("/api/v1/auth/me", headers=hdr)).status_code == 401


async def test_deletion_codes_expire_and_burn_after_five_wrong_tries(tmp_path, monkeypatch, outbox) -> None:
    from remembra.account import deletion

    async with secure_app(tmp_path, SOCIAL_ROUTERS, settings=oauth_settings(resend_api_key="re_test_key")) as h:
        uid = await h.create_user("codes@example.com")
        hdr = h.jwt(uid, "codes@example.com")
        await h.client.post("/api/v1/auth/me/deletion-code", headers=hdr)
        code = outbox.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(deletion.DELETION_CODE_MAX_ATTEMPTS):
            r = await h.client.request("DELETE", "/api/v1/auth/me", json={"code": wrong}, headers=hdr)
            assert r.status_code == 400
        r = await h.client.request("DELETE", "/api/v1/auth/me", json={"code": code}, headers=hdr)
        assert r.status_code == 400  # burned

        await h.client.post("/api/v1/auth/me/deletion-code", headers=hdr)
        code = outbox.last_code()
        monkeypatch.setattr(deletion, "DELETION_CODE_TTL", timedelta(seconds=-1))
        await h.client.post("/api/v1/auth/me/deletion-code", headers=hdr)  # issued already expired
        expired = outbox.last_code()
        r = await h.client.request("DELETE", "/api/v1/auth/me", json={"code": expired}, headers=hdr)
        assert r.status_code == 400
        assert (await h.db.get_user_by_id(uid))["is_active"]


async def test_deletion_code_needs_email_delivery(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    async with secure_app(tmp_path, SOCIAL_ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("noemail@example.com")
        r = await h.client.post("/api/v1/auth/me/deletion-code", headers=h.jwt(uid, "noemail@example.com"))
        assert r.status_code == 503 and "password" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Superadmin: hard delete now, and undo inside the grace period
# ---------------------------------------------------------------------------


async def _owner(c: Any) -> dict[str, str]:
    owner = await c.h.create_user("owner@example.com", verified=True)
    return c.h.jwt(owner, "owner@example.com")


async def test_superadmin_hard_delete_cancels_billing_and_erases_everything(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    store = await _qdrant()
    try:
        async with cost_app(tmp_path) as c:
            _paddle(c, **PRICES)
            c.h.app.include_router(admin.router, prefix="/api/v1")
            c.service.qdrant = store
            c.h.app.state.qdrant = store
            await init_every_schema(c.h.db)
            owner = await _owner(c)
            uid, key = await c.account("gone@example.com")
            await _hook(c, "transaction.completed", _purchase("txn_g", "sub_g", "pri_solo_m", _bound(uid), customer="ctm_g"))
            paddle.add_subscription("sub_g", "ctm_g")
            await _fill_account(c, uid, key, "gone")
            await _connect_oauth(c.h.db, uid)

            paddle.failures["GET /subscriptions/sub_g"] = 500
            r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
            assert r.status_code == 502 and await c.h.db.get_user_by_id(uid) is not None
            paddle.failures.clear()

            r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["subscriptions_cancelled"] == 1 and body["vectors_deleted"] >= 1
            assert body["receipt"] == f"sha256:{erasure_digest(uid)}"
            assert paddle.subscriptions["sub_g"]["status"] == "canceled"
            assert await _cells(c.h.db.conn, [uid, "gone@example.com"]) == {}
            assert await _points(store, uid) == 0
    finally:
        await store.close()


async def test_reactivating_inside_the_grace_period_undoes_the_deletion(tmp_path, monkeypatch) -> None:
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        c.h.app.include_router(admin.router, prefix="/api/v1")
        owner = await _owner(c)
        uid, _key = await c.account("oops@example.com")
        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(uid, "oops@example.com")
        )
        assert r.status_code == 200
        r = await c.h.client.post(f"/api/v1/admin/users/{uid}/activate", params={"active": "true"}, headers=owner)
        assert r.status_code == 200
        user = await c.h.db.get_user_by_id(uid)
        assert user["is_active"] and user["deleted_at"] is None
        eraser = AccountEraser(c.h.db, None)
        assert await eraser.due_accounts(timedelta(0), now=datetime.now(UTC) + timedelta(days=30)) == []
        r = await c.h.client.post("/api/v1/auth/login", json={"email": "oops@example.com", "password": PASSWORD})
        assert r.status_code == 200, r.text


async def test_with_no_grace_period_the_account_is_erased_at_once(tmp_path, monkeypatch) -> None:
    _paddle_mock.install(monkeypatch)
    store = await _qdrant()
    try:
        async with cost_app(tmp_path, account_erasure_grace_days=0) as c:
            c.service.qdrant = store
            c.h.app.state.qdrant = store
            await init_every_schema(c.h.db)
            uid, key = await c.account("now@example.com")
            r = await c.h.client.post("/api/v1/memories", json={"content": "erase me right away"}, headers=key)
            assert r.status_code == 201 and await _points(store, uid) == 1
            r = await c.h.client.request(
                "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(uid, "now@example.com")
            )
            assert r.status_code == 200, r.text
            assert r.json()["erasure_after"] == r.json()["deleted_at"]
            assert await _cells(c.h.db.conn, [uid, "now@example.com"]) == {}
            assert await _points(store, uid) == 0
    finally:
        await store.close()
