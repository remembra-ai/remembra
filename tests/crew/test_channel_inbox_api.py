"""WP-7 REST routes through the real auth chain: channel, decisions, inboxes, cursors, notifications, /inbox scoping.

The app mounts the production routers with authentication ENABLED (real API
keys, real dashboard JWTs), a real crew.db, event log and bus.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from remembra.api.v1 import auth, crew_channel, crew_inbox, inbox
from remembra.auth.rbac import Role
from remembra.crew.access import audit_crew_routes
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.events import CrewEventLog
from remembra.crew.limits import CrewRateLimiter, set_crew_rate_limiter
from remembra.crew.notify import WebhookSender, verify_signature
from remembra.crew.schemas import ROUTES
from remembra.inbox.manager import InboxManager
from tests.crew.crewdb import open_crew_db
from tests.crew.wp7_support import (
    CREW_A,
    CREW_B,
    Receiver,
    add_session,
    events_honour_the_contract,  # noqa: F401 - autouse fixture
    public_resolver,
    seed_crew,
)
from tests.security_harness import make_settings, secure_app

ROUTERS = [crew_channel.router, crew_inbox.router, inbox.router]


@asynccontextmanager
async def api(tmp_path, **settings_overrides: Any):
    db = await open_crew_db(tmp_path)
    bus = CrewBus(loader=db_loader(db))
    log = CrewEventLog(db, bus)
    receiver = Receiver()
    state = {
        "crew_db": db,
        "crew_bus": bus,
        "crew_events": log,
        "crew_webhook_sender": WebhookSender(resolver=public_resolver, transport=receiver.transport()),
    }
    try:
        async with secure_app(tmp_path, [auth.router, *ROUTERS], state=state, settings=make_settings(**settings_overrides)) as h:
            manager = InboxManager(h.db)
            await manager.init_schema()
            h.app.state.inbox_manager = manager
            owner = await h.create_user("owner@example.com")
            other = await h.create_user("other@example.com")
            await seed_crew(db, CREW_A, owner=owner)
            await seed_crew(db, CREW_B, owner=other, project="theirs")
            h.created.update(owner=owner, other=other, db=db, receiver=receiver)
            yield h
    finally:
        await db.close()


async def agent_key(h, user_id: str, agent_id: str | None = None, role: str = "editor", **kw: Any) -> dict[str, str]:
    created = await h.keys.create_key(user_id=user_id, name=f"{agent_id}-key", agent_id=agent_id)
    await h.roles.assign_role(created.id, Role(role), **kw)
    return {"X-API-Key": created.key}


def sess(session_id: str) -> dict[str, str]:
    """The session-token header every crew route reads (the token ``join`` returned)."""
    return {"X-Remembra-Crew-Session": f"tok-{session_id}"}


def test_every_wp7_l0_route_is_registered_with_the_contract_access_rules():
    app = FastAPI()
    for router in ROUTERS:
        app.include_router(router, prefix="/api/v1")
    wp7 = [r for r in ROUTES if r.owner == "WP-7" and r.release == "L0"]
    assert len(wp7) >= 20
    assert audit_crew_routes(app.routes, wp7, require_all=True) == []
    from remembra.crew.access import _walk_routes

    registered = {(m, path) for path, r, _ in _walk_routes(app.routes) for m in r.methods or ()}
    missing = [f"{r.method} {r.path}" for r in wp7 if (r.method, "/api/v1" + r.path) not in registered]
    assert missing == []


async def test_agent_posts_through_its_session_and_humans_through_their_login(tmp_path):
    async with api(tmp_path) as h:
        owner, db = h.created["owner"], h.created["db"]
        key = await agent_key(h, owner, "codex")
        await add_session(db, CREW_A, "cs_a", callsign="codex-1", agent_id="codex", user_id=owner)
        await add_session(db, CREW_A, "cs_b", callsign="cc-1", agent_id="claude-code", user_id=owner)
        url = f"/api/v1/crews/{CREW_A}/messages"
        body = {"kind": "question", "body": "@cc-1 are you in pos?", "client_msg_id": "m1"}

        res = await h.client.post(url, json=body, headers=key)
        assert res.status_code == 401 and res.json()["detail"]["error"] == "session_auth"
        bad = {"X-Remembra-Crew-Session": "wrong"}
        assert (await h.client.post(url, json=body, headers={**key, **bad})).status_code == 401
        # an agent-scoped key cannot speak as another agent's session
        assert (await h.client.post(url, json=body, headers={**key, **sess("cs_b")})).status_code == 401

        res = await h.client.post(url, json=body, headers={**key, **sess("cs_a")})
        assert res.status_code == 201, res.text
        out = res.json()
        assert out["message"]["author_callsign"] == "codex-1" and out["routing"]["sessions"] == ["cs_b"]
        assert out["message"]["author_label"] == "agent codex (key-verified)" and out["seq"] >= 1

        human = h.jwt(owner, "owner@example.com")
        res = await h.client.post(
            url, json={"kind": "answer", "body": "yes", "client_msg_id": "h1", "reply_to_id": out["message"]["id"]}, headers=human
        )
        assert res.status_code == 201 and res.json()["message"]["author_kind"] == "human"

        res = await h.client.post(url, json={**body, "client_msg_id": "x", "kind": "override"}, headers={**key, **sess("cs_a")})
        assert res.status_code == 422 and res.json()["detail"]["error"] == "reserved_sender"
        res = await h.client.post(url, json={**body, "client_msg_id": "y", "from": "mani"}, headers={**key, **sess("cs_a")})
        assert res.status_code == 422  # closed body: no sender field exists

        listed = (await h.client.get(url, headers=key)).json()["items"]
        assert [m["author_kind"] for m in listed] == ["agent", "human"]
        thread = (await h.client.get(url, params={"thread": out["message"]["id"]}, headers=key)).json()["items"]
        assert len(thread) == 2

        edit = await h.client.patch(
            f"/api/v1/messages/{out['message']['id']}", json={"body": "@cc-1 in pos?"}, headers={**key, **sess("cs_a")}
        )
        assert edit.status_code == 200 and edit.json()["message"]["edited_at"]

        # another tenant's crew and entities are invisible (404, never 403)
        other_key = await agent_key(h, h.created["other"], "codex")
        assert (await h.client.get(url, headers=other_key)).status_code == 404
        assert (
            await h.client.patch(f"/api/v1/messages/{out['message']['id']}", json={"body": "x"}, headers=other_key)
        ).status_code == 404


async def test_human_only_routes_refuse_admin_api_keys(tmp_path):
    async with api(tmp_path) as h:
        owner, db = h.created["owner"], h.created["db"]
        admin = await agent_key(h, owner, None, role="admin")
        human = h.jwt(owner, "owner@example.com")
        await add_session(db, CREW_A, "cs_a", callsign="cc-1", user_id=owner, agent_id="claude-code")
        agent = await agent_key(h, owner, "claude-code")
        msg = (
            await h.client.post(
                f"/api/v1/crews/{CREW_A}/messages",
                json={"kind": "chat", "body": "hi", "client_msg_id": "1"},
                headers={**agent, **sess("cs_a")},
            )
        ).json()["message"]
        dec = (
            await h.client.post(
                f"/api/v1/crews/{CREW_A}/decisions", json={"title": "T", "decision": "D"}, headers={**agent, **sess("cs_a")}
            )
        ).json()
        assert dec["state"] == "proposed"
        for path, body in (
            (f"/api/v1/messages/{msg['id']}/redact", None),
            (f"/api/v1/messages/{msg['id']}/pin", None),
            (f"/api/v1/decisions/{dec['id']}/confirm", None),
            (f"/api/v1/decisions/{dec['id']}/reject", None),
            (f"/api/v1/decisions/{dec['id']}/supersede", {"title": "a", "decision": "b"}),
        ):
            res = await h.client.post(path, json=body, headers=admin)
            assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only", (path, res.text)
        assert (await h.client.post(f"/api/v1/messages/{msg['id']}/pin", headers=human)).json()["message"]["pinned"]
        confirmed = await h.client.post(f"/api/v1/decisions/{dec['id']}/confirm", headers=human)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "in_force"
        sup = await h.client.post(
            f"/api/v1/decisions/{dec['id']}/supersede", json={"title": "T2", "decision": "D2"}, headers=human
        )
        assert sup.status_code == 200 and sup.json()["supersedes_id"] == dec["id"]
        red = await h.client.post(f"/api/v1/messages/{msg['id']}/redact", headers=human)
        assert red.status_code == 200 and red.json()["message"]["redacted"]
        states = (await h.client.get(f"/api/v1/crews/{CREW_A}/decisions", params={"state": ["in_force"]}, headers=agent)).json()
        assert [d["title"] for d in states["items"]] == ["T2"]

        # Idempotency-Key on decision creation
        hdr = {**human, "Idempotency-Key": "k1"}
        one = (await h.client.post(f"/api/v1/crews/{CREW_A}/decisions", json={"title": "X", "decision": "Y"}, headers=hdr)).json()
        two = (await h.client.post(f"/api/v1/crews/{CREW_A}/decisions", json={"title": "X", "decision": "Y"}, headers=hdr)).json()
        assert one["id"] == two["id"] and one["state"] == "in_force"
        clash = await h.client.post(f"/api/v1/crews/{CREW_A}/decisions", json={"title": "Z", "decision": "Y"}, headers=hdr)
        assert clash.status_code == 422


async def test_inbox_items_audience_rules_and_cursors(tmp_path):
    async with api(tmp_path) as h:
        owner, db = h.created["owner"], h.created["db"]
        human = h.jwt(owner, "owner@example.com")
        agent = await agent_key(h, owner, "claude-code")
        await add_session(db, CREW_A, "cs_a", callsign="cc-1", user_id=owner)
        await add_session(db, CREW_A, "cs_b", callsign="cc-2", user_id=owner)
        post = f"/api/v1/crews/{CREW_A}/messages"
        await h.client.post(
            post, json={"kind": "question", "body": "@mani rate?", "client_msg_id": "q"}, headers={**agent, **sess("cs_a")}
        )
        crew_msg = (
            await h.client.post(
                post, json={"kind": "status", "body": "@crew free task", "client_msg_id": "c"}, headers={**agent, **sess("cs_a")}
            )
        ).json()

        needs = (await h.client.get(f"/api/v1/crews/{CREW_A}/inbox", headers=human)).json()
        [item] = needs["items"]
        assert item["kind"] == "human_question" and needs["counts"] == {"project": 1, "crew": 1}
        res = await h.client.post(f"/api/v1/inbox/items/{item['id']}/resolve", headers={**agent, **sess("cs_a")})
        assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only"  # an agent cannot clear Needs-you
        assert (await h.client.post(f"/api/v1/inbox/items/{item['id']}/seen", headers=human)).json()["item"]["state"] == "seen"
        assert (await h.client.post(f"/api/v1/inbox/items/{item['id']}/resolve", headers=human)).json()["item"][
            "state"
        ] == "resolved"

        crew_item = crew_msg["routing"]["crew_inbox"]
        claim = await h.client.post(f"/api/v1/inbox/items/{crew_item}/claim", headers={**agent, **sess("cs_b")})
        assert claim.status_code == 200 and claim.json()["item"]["claimed_by"] == "cs_b"
        taken = await h.client.post(f"/api/v1/inbox/items/{crew_item}/claim", headers={**agent, **sess("cs_a")})
        assert taken.status_code == 409 and taken.json()["detail"]["error"] == "already_claimed"
        assert (
            await h.client.post(f"/api/v1/inbox/items/{crew_item}/dismiss", headers={**agent, **sess("cs_b")})
        ).status_code == 403
        assert (
            await h.client.post(f"/api/v1/inbox/items/{crew_item}/resolve", headers={**agent, **sess("cs_b")})
        ).status_code == 200

        mine = (
            await h.client.get(f"/api/v1/crews/{CREW_A}/inbox", params={"audience": "me"}, headers={**agent, **sess("cs_b")})
        ).json()
        assert mine["audience"] == "session" and [i["kind"] for i in mine["items"]] == ["mention"]
        queue_item = mine["items"][0]["id"]
        assert (
            await h.client.post(f"/api/v1/inbox/items/{queue_item}/seen", headers={**agent, **sess("cs_a")})
        ).status_code == 403
        assert (
            await h.client.post(f"/api/v1/inbox/items/{queue_item}/seen", headers={**agent, **sess("cs_b")})
        ).status_code == 200

        read = await h.client.post(
            f"/api/v1/crews/{CREW_A}/read", json={"stream": "messages", "seq": 1}, headers={**agent, **sess("cs_b")}
        )
        assert read.json() == {"stream": "messages", "last_seq": 1, "principal": "cs_b"}
        too_far = await h.client.post(f"/api/v1/crews/{CREW_A}/read", json={"stream": "messages", "seq": 10_000}, headers=human)
        assert too_far.status_code == 422

        overview = (await h.client.get("/api/v1/crews/inbox/overview", headers=human)).json()
        assert overview["crews"] == [{"crew_id": CREW_A, "project_id": "yaadbooks", "needs_you": 0}]
        restricted = await agent_key(h, owner, "claude-code", project_ids=["elsewhere"])
        assert (await h.client.get("/api/v1/crews/inbox/overview", headers=restricted)).json()["crews"] == []


async def test_message_cursors_past_sqlite_integers_are_422_not_500(tmp_path):
    async with api(tmp_path) as h:
        owner = h.created["owner"]
        human = h.jwt(owner, "owner@example.com")
        url = f"/api/v1/crews/{CREW_A}/messages"
        for name in ("since_seq", "before"):
            ok = await h.client.get(url, params={name: 2**63 - 1}, headers=human)
            assert ok.status_code == 200, (name, ok.text)
            for too_big in (2**63, 10**30):
                assert (await h.client.get(url, params={name: too_big}, headers=human)).status_code == 422, name


async def test_notification_targets_rules_and_in_app_list(tmp_path):
    async with api(tmp_path) as h:
        owner, db = h.created["owner"], h.created["db"]
        human = h.jwt(owner, "owner@example.com")
        admin = await agent_key(h, owner, None, role="admin")
        body = {"kind": "webhook", "target": "https://hooks.example.com/r"}
        assert (await h.client.post("/api/v1/notifications/targets", json=body, headers=admin)).status_code == 403
        res = await h.client.post("/api/v1/notifications/targets", json=body, headers=human)
        assert res.status_code == 201, res.text
        target = res.json()
        assert target["signing_secret"].startswith("whsec_") and target["crews_without_channel"] == [CREW_A]
        [challenge] = h.created["receiver"].requests
        assert verify_signature(target["signing_secret"], challenge.content, challenge.headers["X-Remembra-Signature"])
        bad = await h.client.post(
            "/api/v1/notifications/targets", json={"kind": "webhook", "target": "http://x.example.com"}, headers=human
        )
        assert bad.status_code == 422

        rules = (await h.client.get("/api/v1/notifications/rules", headers=human)).json()
        assert {r["kind"] for r in rules["defaults"] if r["realtime"]} >= {"handoff", "collision", "tamper", "zone_change"}
        assert [t["kind"] for t in rules["targets"]] == ["webhook"] and "signing_secret" not in rules["targets"][0]
        assert rules["targets"][0]["target"] == "https://hooks.example.com/r"  # the person who manages them sees them
        # human-managed, and not readable in full by a key: a webhook path often carries a token
        for key in (
            admin,
            await agent_key(h, owner, "codex", role="editor"),
            await agent_key(h, owner, None, role="viewer", project_ids=["some-other-project"]),
        ):
            seen = (await h.client.get("/api/v1/notifications/rules", headers=key)).json()["targets"]
            assert [(t["kind"], t["target"]) for t in seen] == [("webhook", "https://hooks.example.com/…")], seen
            assert seen[0]["id"] == rules["targets"][0]["id"] and "verified_at" in seen[0]

        a = await add_session(db, CREW_A, "cs_a", callsign="cc-1", user_id=owner)
        await h.app.state.crew_events.emit(
            crew_id=CREW_A,
            type="guard.tamper_blocked",
            actor=a.actor(),
            payload={"kind": "crewd_kill", "surface": "pretool"},
            summary="tamper",
            refs={"session_id": "cs_a"},
        )
        listed = (await h.client.get("/api/v1/notifications", headers=human)).json()
        assert listed["unread"] == 1 and listed["items"][0]["text"] == "cc-1 tried to stop the crew daemon. Blocked."
        marked = await h.client.patch("/api/v1/notifications", json={"all": True}, headers=human)
        assert marked.status_code == 200 and marked.json()["unread"] == 0
        assert (await h.client.patch("/api/v1/notifications", json={"crew_id": CREW_B}, headers=human)).status_code == 404
        assert (await h.client.patch("/api/v1/notifications", json={}, headers=human)).status_code == 422


async def test_messages_rate_limit_is_per_session(tmp_path):
    set_crew_rate_limiter(CrewRateLimiter("memory://"))
    try:
        async with api(tmp_path, rate_limit_enabled=True) as h:
            owner, db = h.created["owner"], h.created["db"]
            agent = await agent_key(h, owner, "claude-code")
            await add_session(db, CREW_A, "cs_a", callsign="cc-1", user_id=owner)
            await add_session(db, CREW_A, "cs_b", callsign="cc-2", user_id=owner)
            url = f"/api/v1/crews/{CREW_A}/messages"
            for i in range(20):
                res = await h.client.post(
                    url, json={"kind": "chat", "body": "x", "client_msg_id": f"a{i}"}, headers={**agent, **sess("cs_a")}
                )
                assert res.status_code == 201, res.text
            res = await h.client.post(
                url, json={"kind": "chat", "body": "x", "client_msg_id": "a20"}, headers={**agent, **sess("cs_a")}
            )
            assert res.status_code == 429 and res.json()["detail"]["retry_after_s"] >= 1
            other = await h.client.post(
                url, json={"kind": "chat", "body": "x", "client_msg_id": "b0"}, headers={**agent, **sess("cs_b")}
            )
            assert other.status_code == 201  # another session of the same user is not starved
    finally:
        set_crew_rate_limiter(None)


# ---------------------------------------------------------------------------
# /inbox: reserved senders and project scoping (main-DB agent_inbox, v5)
# ---------------------------------------------------------------------------


async def test_inbox_reserved_senders_and_provenance(tmp_path):
    async with api(tmp_path) as h:
        owner = h.created["owner"]
        plain = await agent_key(h, owner, None)
        scoped = await agent_key(h, owner, "codex")
        human = h.jwt(owner, "owner@example.com")
        send = "/api/v1/inbox/send"
        base = {"to_agent": "claude-code", "subject": "s", "body": "b"}
        for bad in (
            {"from_agent": "mani"},
            {"from_agent": " System "},
            {"from_agent": "remembra"},
            # look-alikes (review finding): zero-width, Cyrillic a, punctuation, a qualifier, fullwidth
            {"from_agent": "Mani\u200b"},
            {"from_agent": "M\u0430ni"},
            {"from_agent": "mani."},
            {"from_agent": "Mani (owner)"},
            {"from_agent": "\uff4d\uff41\uff4e\uff49"},
            {"kind": "override"},
            {"kind": "pause"},
        ):
            res = await h.client.post(send, json={**base, **bad}, headers=plain)
            assert res.status_code == 422 and res.json()["detail"]["error"] == "reserved_sender", bad
        mani_key = await agent_key(h, owner, "mani")
        assert (await h.client.post(send, json=base, headers=mani_key)).status_code == 422

        self_declared = (await h.client.post(send, json={**base, "from_agent": "gemini"}, headers=plain)).json()
        assert self_declared["sender_kind"] == "agent" and self_declared["sender_verified"] is False
        verified = (await h.client.post(send, json={**base, "from_agent": "pretend"}, headers=scoped)).json()
        assert verified["sender_verified"] is True
        from_human = (await h.client.post(send, json={**base, "from_agent": "mani", "kind": "pause"}, headers=human)).json()
        assert from_human["sender_kind"] == "human"
        rows = (await h.client.get("/api/v1/inbox", params={"agent_id": "claude-code"}, headers=plain)).json()
        labels = sorted(r["sender_label"] for r in rows)
        assert labels == ["agent codex (key-verified)", "agent gemini (self-declared)", "human"]


async def test_inbox_project_scoping_for_restricted_keys(tmp_path):
    async with api(tmp_path) as h:
        owner = h.created["owner"]
        wide = await agent_key(h, owner, None)
        p1 = await agent_key(h, owner, None, project_ids=["p1"])
        both = await agent_key(h, owner, None, project_ids=["p1", "p2"])
        send = "/api/v1/inbox/send"
        base = {"to_agent": "codex", "subject": "s", "body": "b", "from_agent": "cc"}
        untagged = (await h.client.post(send, json=base, headers=wide)).json()
        in_p2 = (await h.client.post(send, json={**base, "project_id": "p2"}, headers=wide)).json()
        auto = (await h.client.post(send, json=base, headers=p1)).json()
        assert untagged["project_id"] is None and auto["project_id"] == "p1"
        assert (await h.client.post(send, json={**base, "project_id": "p2"}, headers=p1)).status_code == 403
        needs = await h.client.post(send, json=base, headers=both)
        assert needs.status_code == 422 and needs.json()["detail"]["error"] == "project_required"

        def ids(res):
            return sorted(r["inbox_id"] for r in res.json())

        q = {"agent_id": "codex", "status": "all"}
        assert ids(await h.client.get("/api/v1/inbox", params=q, headers=p1)) == [auto["inbox_id"]]  # NULL and p2 hidden
        assert len((await h.client.get("/api/v1/inbox", params=q, headers=wide)).json()) == 3
        unscoped = await h.client.get("/api/v1/inbox", params={**q, "scope": "unscoped"}, headers=wide)
        assert ids(unscoped) == [untagged["inbox_id"]]
        assert (await h.client.get("/api/v1/inbox", params={**q, "scope": "unscoped"}, headers=p1)).json() == []
        only_p2 = await h.client.get("/api/v1/inbox", params={**q, "project_id": "p2"}, headers=wide)
        assert ids(only_p2) == [in_p2["inbox_id"]]
        listing = (await h.client.get("/api/v1/inbox/messages", params={"status": "all"}, headers=p1)).json()
        assert listing["total"] == 1
        summary = (await h.client.get("/api/v1/inbox/summary", headers=p1)).json()
        assert summary["unread_total"] == 1
        hidden = await h.client.post(f"/api/v1/inbox/{untagged['inbox_id']}/ack", json={}, headers=p1)
        assert hidden.status_code == 404
        assert (
            await h.client.post(f"/api/v1/inbox/{auto['inbox_id']}/ack", json={"result": "done"}, headers=p1)
        ).status_code == 200


async def test_email_targets_are_confirmed_by_a_mailed_code_over_http(tmp_path):
    """Review fix: an email target other than the account's own verified address is saved unverified and a
    fixed-template code is mailed to it; ``POST /notifications/targets/{id}/confirm`` (human only) verifies it."""
    import re

    from remembra.cloud.email import EmailResult

    class Mail:
        sent: list[Any] = []

        async def send(self, message: Any) -> EmailResult:
            self.sent.append(message)
            return EmailResult(success=True, message_id="m")

    async with api(tmp_path) as h:
        owner = h.created["owner"]
        human = h.jwt(owner, "owner@example.com")
        admin = await agent_key(h, owner, None, role="admin")
        mail = Mail()
        h.app.state.crew_email_backend = mail
        res = await h.client.post(
            "/api/v1/notifications/targets", json={"kind": "email", "target": "x@third.example"}, headers=human
        )
        assert res.status_code == 201, res.text
        target = res.json()
        assert target["verified_at"] is None and target["confirmation"] == "sent"
        code = re.search(r"<strong>(\w+)</strong>", mail.sent[0].html).group(1)
        url = f"/api/v1/notifications/targets/{target['id']}/confirm"
        assert (await h.client.post(url, json={"code": code}, headers=admin)).status_code == 403  # human only
        assert (await h.client.post(url, json={"code": "ZZZZZZZZ"}, headers=human)).status_code == 422
        ok = await h.client.post(url, json={"code": code}, headers=human)
        assert ok.status_code == 200 and ok.json()["verified_at"], ok.text
        rules = (await h.client.get("/api/v1/notifications/rules", headers=human)).json()
        assert [t["verified_at"] is not None for t in rules["targets"]] == [True]
