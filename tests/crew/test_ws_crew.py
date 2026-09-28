"""WP-2 WebSocket crew streams on the real /ws endpoint (real auth, real crew.db, real bus)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import anyio
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from remembra.api.v1 import keys as keys_api
from remembra.api.v1 import websocket
from remembra.crew import schemas
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.events import Actor, CrewEventLog, crew_head
from tests.crew.crewdb import CREW_A, CREW_B, open_crew_db, seed_crew, seed_member, seed_session, state_changed
from tests.security_harness import secure_app

CREW_SCOPES = ["memory:recall", "crew:read", "crew:write"]


@asynccontextmanager
async def crew_app(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    crewdb = await open_crew_db(tmp_path)
    await seed_crew(crewdb, CREW_A, owner="owner-1", project="yaadbooks")
    await seed_crew(crewdb, CREW_B, owner="owner-2", project="other")
    async with secure_app(tmp_path, [websocket.router], prefix="", state={"crew_db": crewdb}) as h:
        h.app.include_router(keys_api.router, prefix="/api/v1")
        bus = CrewBus(loader=db_loader(crewdb))
        detach = websocket.connection_manager.attach_crew_bus(bus)
        try:
            yield h, crewdb, CrewEventLog(crewdb, bus)
        finally:
            detach()
            await crewdb.close()


def recv(ws, timeout=3.0):
    async def _receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(_receive)
    ws._raise_on_close(message)
    return json.loads(message["text"]) if message["text"] not in ("ping", "pong") else message["text"]


def recv_type(ws, want, limit=10, timeout=3.0):
    for _ in range(limit):
        msg = recv(ws, timeout)
        if isinstance(msg, dict) and msg.get("type") == want:
            return msg
    raise AssertionError(f"no {want} frame")


def expect_silence(ws, timeout=0.3):
    with pytest.raises(TimeoutError):
        recv(ws, timeout)


def expect_close(ws, code, timeout=3.0):
    with pytest.raises(WebSocketDisconnect) as exc:
        for _ in range(20):
            recv(ws, timeout)
    assert exc.value.code == code


def sub(crew_id=CREW_A, since=None, topics=("crew",)):
    msg = {"type": "subscribe", "channel": "crew", "crew_id": crew_id, "topics": list(topics)}
    if since is not None:
        msg["since_seq"] = since
    return json.dumps(msg)


def emit(client, log, n=1, crew_id=CREW_A, **over):
    out = []
    for _ in range(n):
        kwargs = dict(
            crew_id=crew_id,
            type="session.state_changed",
            actor=Actor.system(),
            payload=state_changed(),
            summary="state changed",
        )
        kwargs.update(over)
        out.append(client.portal.call(lambda kw=kwargs: log.emit(**kw)))
    return out


def connect(client, key):
    ws = client.websocket_connect("/ws", headers={"X-API-Key": key})
    return ws


async def test_replay_then_live_strictly_by_seq_with_valid_frames(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client:
            emit(client, log, 5)
            with connect(client, key) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub(since=2))
                subscribed = recv(ws)
                assert subscribed == {"type": "crew.subscribed", "crew_id": CREW_A, "since_seq": 2, "replayed": 3}
                assert schemas.validate_ws_frame(subscribed) == []
                replay = [recv(ws) for _ in range(3)]
                emit(client, log, 2)
                live = [recv(ws) for _ in range(2)]
                frames = replay + live
                assert [f["data"]["seq"] for f in frames] == [3, 4, 5, 6, 7]
                for f in frames:
                    assert f["type"] == "crew.event" and schemas.validate_ws_frame(f) == []
                # another crew's events never arrive
                emit(client, log, 1, crew_id=CREW_B)
                expect_silence(ws)
                # subscribing without since_seq streams live only
                ws.send_text(sub())
                assert recv(ws)["replayed"] == 0
                emit(client, log, 1)
                assert recv(ws)["data"]["seq"] == 8


async def test_large_gap_and_future_cursor_require_resync(tmp_path, monkeypatch):
    assert websocket.REPLAY_MAX == 500
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client:

            async def bulk():
                async with log.transaction() as tx:
                    for _ in range(501):
                        await tx.emit(
                            **dict(
                                crew_id=CREW_A,
                                type="session.state_changed",
                                actor=Actor.system(),
                                payload=state_changed(),
                                summary="bulk",
                            )
                        )

            client.portal.call(bulk)
            with connect(client, key) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub(since=0))  # 501 behind: more than 500
                frame = recv(ws)
                assert frame == {"type": "resync_required", "crew_id": CREW_A, "reason": "gap_too_large", "last_seq": 501}
                assert schemas.validate_ws_frame(frame) == []
                ws.send_text(sub(since=1))  # exactly 500 behind: replayed
                assert recv(ws)["replayed"] == 500
                assert [recv(ws)["data"]["seq"] for _ in range(500)] == list(range(2, 502))
                ws.send_text(sub(since=999))
                assert recv(ws)["reason"] == "server_restart"
                # after a resync the client resubscribes from its snapshot's as_of_seq
                ws.send_text(sub(since=501))
                assert recv(ws)["replayed"] == 0
                emit(client, log, 1)
                assert recv(ws)["data"]["seq"] == 502


async def test_membership_project_restriction_and_permission_gates(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        await seed_member(crewdb, CREW_A, "member-1", "member")
        stranger, _ = await h.api_key("stranger", "editor", scopes=CREW_SCOPES)
        member, _ = await h.api_key("member-1", "viewer", scopes=["crew:read"])  # crew-only key
        restricted, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES, project_ids=["other"])
        memory_only, _ = await h.api_key("owner-1", "editor", scopes=["memory:recall"])
        with TestClient(h.app) as client:
            with connect(client, stranger) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                err = recv(ws)
                assert err["type"] == "error" and err["data"]["code"] == "not_found"
                ws.send_text(sub("crw_doesnotexist0000"))
                assert recv(ws)["data"]["code"] == "not_found"  # indistinguishable from forbidden
            with connect(client, restricted) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                assert recv(ws)["data"]["code"] == "not_found"
            with connect(client, memory_only) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                assert recv(ws)["data"]["code"] == "forbidden"
            with connect(client, member) as ws:
                connected = recv_type(ws, "connected")
                assert connected["data"]["project_id"] is None
                ws.send_text(sub())
                assert recv(ws)["type"] == "crew.subscribed"
                # a crew-only key gets no memory stream
                ws.send_text(json.dumps({"type": "subscribe", "project_id": "yaadbooks"}))
                assert recv(ws)["data"]["message"] == "memory:recall permission required"
                sent = client.portal.call(
                    lambda: websocket.connection_manager.broadcast("memory.created", {"x": 1}, user_id="member-1")
                )
                assert sent == 0
            # malformed subscribe
            with connect(client, member) as ws:
                recv_type(ws, "connected")
                ws.send_text(json.dumps({"type": "subscribe", "channel": "crew", "crew_id": CREW_A, "topics": ["crew"], "x": 1}))
                assert recv(ws)["data"]["code"] == "invalid"
                ws.send_text(sub(topics=("crew.summary",)))
                assert recv(ws)["data"]["code"] == "invalid"


async def test_query_string_credentials_are_refused_for_crew_but_work_for_memory(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client:
            with client.websocket_connect(f"/ws?api_key={key}") as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                assert recv(ws)["data"]["code"] == "query_credentials"
                sent = client.portal.call(
                    lambda: websocket.connection_manager.broadcast("memory.created", {"m": 1}, user_id="owner-1")
                )
                assert sent == 1
                assert recv(ws)["type"] == "memory.created"
            # first-message credentials are fine
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"type": "auth", "api_key": key}))
                recv_type(ws, "connected")
                ws.send_text(sub())
                assert recv(ws)["type"] == "crew.subscribed"


async def test_jwt_dashboard_login_can_subscribe(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        user_id = await h.create_user("mani@example.com")
        async with crewdb.transaction():
            await crewdb.conn.execute("UPDATE crews SET owner_user_id = ? WHERE id = ?", (user_id, CREW_A))
        headers = h.jwt(user_id, "mani@example.com")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers=headers) as ws:
            recv_type(ws, "connected")
            ws.send_text(sub(since=0))
            assert recv(ws)["type"] == "crew.subscribed"


async def test_summary_counts_only_filtered_and_updated(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "SUMMARY_DEBOUNCE_S", 0.05)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        await seed_member(crewdb, CREW_B, "owner-1", "member")
        await seed_session(crewdb, CREW_A, "cs_one")
        await seed_session(crewdb, CREW_A, "cs_two", callsign="cc-2")
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        restricted, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES, project_ids=["other"])
        with TestClient(h.app) as client, connect(client, key) as ws, connect(client, restricted) as rws:
            recv_type(ws, "connected")
            recv_type(rws, "connected")
            ws.send_text(sub("*", topics=("crew.summary",)))
            assert recv(ws) == {"type": "crew.subscribed", "crew_id": "*", "since_seq": 0, "replayed": 0}
            summary = recv(ws)
            assert schemas.validate_ws_frame(summary) == []
            assert summary["crews"] == [
                {"crew_id": CREW_A, "project_id": "yaadbooks", "mode": "multi", "live": 2, "moments": 0, "needs_you": 0},
                {"crew_id": CREW_B, "project_id": "other", "mode": "solo", "live": 0, "moments": 0, "needs_you": 0},
            ]
            rws.send_text(sub("*", topics=("crew.summary",)))
            recv(rws)
            assert [c["crew_id"] for c in recv(rws)["crews"]] == [CREW_B]
            # a moment in crew A pushes an updated count to the unrestricted principal only
            emit(client, log, 1, type="host.unreachable", payload={"host_id": "hst_1", "silent_s": 1, "session_ids": []})
            update = recv(ws)
            assert update["type"] == "crew.summary" and update["crews"][0]["moments"] == 1
            assert all("summary" not in c for c in update["crews"])  # counts only, never events
            expect_silence(rws)


async def test_key_revoked_mid_connection_closes_4001_by_revalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 0.2)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, key_id = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, key) as ws:
            recv_type(ws, "connected")
            ws.send_text(sub())
            recv(ws)

            async def switch_off():  # directly in the DB: no hook runs, so no push
                await h.db.conn.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,))
                await h.db.conn.commit()

            # the 30 s (here 0.2 s) re-validation must catch it; a key that no longer authenticates is 4001
            client.portal.call(switch_off)
            expect_close(ws, websocket.CLOSE_UNAUTHORIZED)


async def test_revoked_key_gets_no_further_crew_frame(tmp_path, monkeypatch):
    """The per-frame check (P-348): no hook and no periodic check ran, yet the next crew event is not sent."""
    monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 3600)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, key_id = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, key) as ws:
            recv_type(ws, "connected")
            ws.send_text(sub())
            recv(ws)
            emit(client, log, 1)
            assert recv(ws)["type"] == "crew.event"

            async def switch_off():
                await h.db.conn.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,))
                await h.db.conn.commit()

            client.portal.call(switch_off)
            emit(client, log, 1)
            expect_close(ws, websocket.CLOSE_UNAUTHORIZED)


async def test_rest_key_revocation_pushes_an_immediate_close(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, key_id = await h.api_key("owner-1", "admin", scopes=CREW_SCOPES + ["key:revoke"])
        other, other_id = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, other) as ws, connect(client, key) as keep:
            recv_type(ws, "connected")
            recv_type(keep, "connected")
            ws.send_text(sub())
            recv(ws)
            r = client.delete(f"/api/v1/keys/{other_id}", headers={"X-API-Key": key})
            assert r.status_code == 200, r.text
            expect_close(ws, websocket.CLOSE_UNAUTHORIZED, timeout=1.0)  # the key no longer authenticates
            keep.send_text("ping")
            assert recv(keep) == "pong"  # other connections are untouched


async def test_membership_loss_closes_4003_on_revalidation_and_on_push(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 0.2)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        await seed_member(crewdb, CREW_A, "member-1")
        await seed_member(crewdb, CREW_A, "member-2")
        k1, _ = await h.api_key("member-1", "editor", scopes=CREW_SCOPES)
        k2, _ = await h.api_key("member-2", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client:
            with connect(client, k1) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                recv(ws)

                async def remove_member():
                    async with crewdb.transaction():
                        await crewdb.conn.execute("DELETE FROM crew_members WHERE user_id = 'member-1'")

                client.portal.call(remove_member)
                expect_close(ws, websocket.CLOSE_FORBIDDEN)
            monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 3600)
            with connect(client, k2) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                recv(ws)
                closed = client.portal.call(
                    lambda: websocket.connection_manager.revoke(user_id="member-2", crew_id=CREW_A, reason="removed")
                )
                assert closed == 1
                expect_close(ws, websocket.CLOSE_FORBIDDEN, timeout=1.0)


async def test_queue_overflow_sends_resync_and_drops_the_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "CREW_QUEUE_MAX", 3)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        envs = []
        with TestClient(h.app) as client:
            envs = [r.envelope for r in emit(client, log, 1)]
            with connect(client, key) as ws:
                recv_type(ws, "connected")
                ws.send_text(sub())
                recv(ws)
                fake = [{**envs[0], "seq": 100 + i} for i in range(10)]

                def flood():
                    for env in fake:  # synchronous: the pump cannot drain in between
                        websocket.connection_manager.dispatch_crew_event(env)

                client.portal.call(flood)
                frame = recv(ws)
                assert frame["type"] == "resync_required" and frame["reason"] == "overflow"
                emit(client, log, 1)
                expect_silence(ws)  # the subscription was dropped until the client resubscribes


async def test_presence_frames_server_state_throttle_and_never_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "PRESENCE_MIN_INTERVAL_S", 0.4)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        await seed_session(crewdb, CREW_A, "cs_mine", state="idle")
        await seed_session(crewdb, CREW_A, "cs_theirs", user_id="member-9", callsign="cc-9", state="active")
        crewd, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        viewer, _ = await h.api_key("owner-1", "viewer", scopes=["crew:read"])

        def lane(sid, path="src/app/pos/Receipt.tsx", state="active"):
            return {
                "session_id": sid,
                "state": state,
                "stuck": False,
                "last_action": {"tool": "Edit", "path_rel": path, "age_s": 3},
                "calls_since_checkpoint": 17,
                "next_checkpoint_due_at": None,
                "limit": {"level": "warn", "pct": 0.82, "source": "reported"},
            }

        with TestClient(h.app) as client:
            with connect(client, viewer) as dash, connect(client, crewd) as daemon:
                recv_type(dash, "connected")
                recv_type(daemon, "connected")
                dash.send_text(sub())
                recv(dash)
                daemon.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_mine")]}))
                frame = recv(dash)
                assert schemas.validate_ws_frame(frame) == []
                assert frame["lanes"][0]["state"] == "idle"  # server state, not the client's "active"
                assert frame["lanes"][0]["last_action"]["path_rel"] == "src/app/pos/Receipt.tsx"
                # within 5 s (here 0.4 s): held, then the latest lane is flushed once
                daemon.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_mine", "a.ts")]}))
                daemon.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_mine", "b.ts")]}))
                expect_silence(dash, 0.2)
                flushed = recv(dash, 2.0)
                assert [x["last_action"]["path_rel"] for x in flushed["lanes"]] == ["b.ts"]
                expect_silence(dash, 0.6)
                # another user's session, an absolute path, a command string and a viewer key are all refused
                daemon.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_theirs")]}))
                assert recv_type(daemon, "error")["data"]["session_ids"] == ["cs_theirs"]
                daemon.send_text(
                    json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_mine", "/Users/mani/x.ts")]})
                )
                assert recv_type(daemon, "error")["data"]["code"] == "invalid"
                bad = lane("cs_mine")
                bad["last_action"]["command"] = "curl -H 'Authorization: x'"
                daemon.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [bad]}))
                assert recv_type(daemon, "error")["data"]["code"] == "invalid"
                dash.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": [lane("cs_mine")]}))
                assert recv_type(dash, "error")["data"]["code"] == "forbidden"
                expect_silence(dash, 0.2)
            head = client.portal.call(lambda: crew_head(crewdb.conn, CREW_A))
            assert head.last_seq == 0  # presence is never an event


async def test_subscription_connection_and_replay_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "MAX_CREW_SUBS_PER_CONNECTION", 2)
    monkeypatch.setattr(websocket, "MAX_CREW_CONNECTIONS_PER_USER", 2)
    monkeypatch.setattr(websocket, "REPLAY_SUBSCRIBES_PER_MINUTE", 3)
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        crew_c = "crw_cccccccccccccccc"
        await seed_crew(crewdb, crew_c, owner="owner-1", project="third")
        await seed_member(crewdb, CREW_B, "owner-1")
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, key) as a, connect(client, key) as b, connect(client, key) as c:
            for ws in (a, b, c):
                recv_type(ws, "connected")
            a.send_text(sub(CREW_A))
            assert recv(a)["type"] == "crew.subscribed"
            a.send_text(sub(CREW_B))
            assert recv(a)["type"] == "crew.subscribed"
            a.send_text(sub(crew_c))
            assert recv(a)["data"]["code"] == "limit"
            a.send_text(sub(CREW_A))  # resubscribing an existing crew replaces it
            assert recv(a)["type"] == "crew.subscribed"
            b.send_text(sub(CREW_A))
            assert recv(b)["type"] == "crew.subscribed"
            c.send_text(sub(CREW_A))  # third crew connection for this user
            assert recv(c)["data"]["code"] == "limit"
            b.send_text(sub(CREW_B, since=0))
            b.send_text(sub(CREW_B, since=0))
            b.send_text(sub(CREW_B, since=0))
            for _ in range(3):
                assert recv(b)["type"] == "crew.subscribed"
            b.send_text(sub(CREW_B, since=0))
            err = recv(b)
            assert err["data"]["code"] == "rate_limited" and err["data"]["retry_after_s"] > 0
            # unsubscribing frees the slot
            a.send_text(json.dumps({"type": "unsubscribe", "channel": "crew", "crew_id": CREW_B}))
            a.send_text(sub(crew_c))
            assert recv(a)["type"] == "crew.subscribed"


async def test_with_crew_mode_off_crew_frames_get_no_crew_answer(tmp_path, monkeypatch):
    """Flag off: /ws must not announce the unreleased feature. A crew subscribe gets the generic answer any
    unknown channel gets, a presence frame gets nothing, and no frame mentions crews."""
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, key) as ws:
            recv_type(ws, "connected")
            ws.send_text(json.dumps({"type": "subscribe", "channel": "nope", "crew_id": CREW_A, "topics": ["crew"]}))
            generic = recv(ws)
            ws.send_text(sub())
            answer = recv(ws)
            assert {**answer, "timestamp": None} == {**generic, "timestamp": None}
            ws.send_text(sub("*", topics=("crew.summary",)))
            assert "crew" not in json.dumps(recv(ws)).lower()
            ws.send_text(json.dumps({"type": "presence", "crew_id": CREW_A, "lanes": []}))
            ws.send_text(json.dumps({"type": "unsubscribe", "channel": "crew", "crew_id": CREW_A}))
            expect_silence(ws)


async def test_pump_failure_tells_the_client_to_resync(tmp_path, monkeypatch):
    async with crew_app(tmp_path, monkeypatch) as (h, crewdb, log):
        key, _ = await h.api_key("owner-1", "editor", scopes=CREW_SCOPES)
        with TestClient(h.app) as client, connect(client, key) as ws:
            recv_type(ws, "connected")
            ws.send_text(sub())
            assert recv(ws)["replayed"] == 0

            async def broken_fetch(*args, **kwargs):
                raise RuntimeError("disk gone")

            monkeypatch.setattr(websocket, "fetch_events", broken_fetch)
            envs = [r.envelope for r in emit(client, log, 1)]
            assert recv(ws)["data"]["seq"] == 1
            # a gap forces a DB back-fill, which fails: the client is told to resync, not left hanging
            client.portal.call(lambda: websocket.connection_manager.dispatch_crew_event({**envs[0], "seq": 5}))
            frame = recv(ws)
            assert frame == {"type": "resync_required", "crew_id": CREW_A, "reason": "overflow", "last_seq": 1}
