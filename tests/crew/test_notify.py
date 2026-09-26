"""WP-7 notifications: classification, copy, signed webhooks, email, batching, quiet hours and the loss rule.

Runtime tests: real crew.db, real event log, real outbox worker and the real
signing/pinning code; the network edge is an ``httpx.MockTransport`` receiver.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from remembra.cloud.email import EmailBackend, EmailMessage, EmailResult
from remembra.core.tasks import TaskRegistry
from remembra.crew import notify, startup
from remembra.crew.events import Actor, format_ts
from remembra.crew.notify import (
    KIND_CREW_NOTIFY,
    LOSS_QUIET_S,
    Lookup,
    NotificationDispatcher,
    NotifyError,
    NotifyTargets,
    WebhookSender,
    classify,
    list_notifications,
    notify_handler,
    quiet_until,
    render,
    secret_hash,
    target_secret,
    verify_signature,
)
from remembra.crew.outbox import CrewOutboxWorker
from remembra.crew.store import CrewStore
from tests.crew.wp7_support import (
    CREW_A,
    OWNER,
    Receiver,
    add_claim,
    add_session,
    add_zone,
    events_honour_the_contract,  # noqa: F401 - autouse fixture
    make_env,
    public_resolver,
    set_realtime,
)

HOOK_URL = "https://hooks.example.com/remembra"


class FakeEmail(EmailBackend):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailResult:
        self.sent.append(message)
        return EmailResult(success=True, message_id=f"em-{len(self.sent)}")


async def _quota_event(env, actor, *, claims=("clm_pos",)):
    return await env.log.emit(
        crew_id=CREW_A,
        type="session.quota_blocked",
        actor=actor,
        payload={"error": "billing_error", "source": "reported", "baton_ref": None, "claims_reserved": list(claims)},
        summary=f"{actor.callsign} quota blocked",
        refs={"session_id": actor.id},
    )


async def _setup(tmp_path, *, channels=("email", "webhook")):
    env = await make_env(tmp_path)
    author = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1")
    await add_zone(env.db, CREW_A, "zn_pos", "pos")
    await add_claim(env.db, CREW_A, "clm_pos", "zn_pos", "cs_a", state="reserved")
    await set_realtime(env.db, CREW_A, list(channels))
    receiver = Receiver()
    sender = WebhookSender(resolver=public_resolver, transport=receiver.transport())
    targets = NotifyTargets(env.db, webhooks=sender)
    hook = await targets.add(OWNER, "webhook", HOOK_URL)
    mail = await targets.add(OWNER, "email", "Mani@Example.com")
    return env, author.actor(), receiver, sender, hook, mail


def test_classify_table():
    assert classify({"type": "session.quota_blocked"}) == "handoff"
    assert classify({"type": "session.lost"}) == "handoff"
    assert classify({"type": "collision.detected", "payload": {"collision": {"severity": "critical"}}}) == "collision"
    assert classify({"type": "collision.detected", "payload": {"collision": {"severity": "medium"}}}) is None
    assert classify({"type": "guard.tamper_blocked"}) == "tamper"
    assert classify({"type": "guard.bypass_used"}) == "bypass"
    assert classify({"type": "githook.missing", "payload": {"state": "missing"}}) == "githook"
    assert classify({"type": "githook.missing", "payload": {"state": "ok"}}) is None
    assert classify({"type": "zone.change_pending"}) == "zone_change"
    assert classify({"type": "decision.proposed"}) == "decision"
    assert classify({"type": "session.stuck", "payload": {"stuck": True}}) == "stuck"
    assert classify({"type": "task.done"}) == "task_done"
    assert classify({"type": "message.posted"}) is None


def test_quiet_hours_in_eastern_time():
    # 03:00 UTC on 2026-09-26 is 23:00 EDT on the 25th: inside 22:00-07:00, ends 07:00 EDT = 11:00 UTC.
    now = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
    assert quiet_until(now, "22:00-07:00") == datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
    # 12:00 EDT: outside
    assert quiet_until(datetime(2026, 9, 26, 16, 0, tzinfo=UTC), "22:00-07:00") is None
    # same-day window 12:00-13:00 EDT at 12:30 EDT
    assert quiet_until(datetime(2026, 9, 26, 16, 30, tzinfo=UTC), "12:00-13:00") == datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
    assert quiet_until(now, None) is None and quiet_until(now, "garbage") is None


async def test_webhook_target_needs_https_ssrf_safety_and_a_signed_challenge(tmp_path):
    env = await make_env(tmp_path)
    receiver = Receiver()
    targets = NotifyTargets(env.db, webhooks=WebhookSender(resolver=public_resolver, transport=receiver.transport()))
    out = await targets.add(OWNER, "webhook", HOOK_URL)
    assert out["verified_at"] and out["signing_secret"] == target_secret(out["id"])
    challenge = receiver.requests[0]
    assert challenge.url.host == "93.184.216.34" and challenge.headers["host"] == "hooks.example.com"  # pinned IP
    assert verify_signature(out["signing_secret"], challenge.content, challenge.headers["X-Remembra-Signature"])
    row = await env.db.fetchone("SELECT * FROM crew_notify_targets WHERE id = ?", (out["id"],))
    assert row["secret_hash"] == secret_hash(out["signing_secret"]) and out["signing_secret"] not in json.dumps(dict(row))
    again = await targets.add(OWNER, "webhook", HOOK_URL)
    assert again["id"] == out["id"] and len(await targets.list(OWNER)) == 1

    with pytest.raises(NotifyError):
        await targets.add(OWNER, "webhook", "http://hooks.example.com/x")
    with pytest.raises(NotifyError):  # the real SSRF resolver refuses loopback (no network needed)
        await NotifyTargets(env.db).add(OWNER, "webhook", "https://127.0.0.1/hook")
    silent = NotifyTargets(env.db, webhooks=WebhookSender(resolver=public_resolver, transport=Receiver(echo=False).transport()))
    with pytest.raises(NotifyError):
        await silent.add(OWNER, "webhook", "https://other.example.com/hook")
    assert len(await targets.list(OWNER)) == 1  # the failed one was not stored
    with pytest.raises(NotifyError):
        await targets.add(OWNER, "email", "not-an-email")
    mail = await targets.add(OWNER, "email", "Mani@Example.com")
    assert mail["target"] == "mani@example.com" and "signing_secret" not in mail


async def test_quota_stop_reaches_webhook_and_email_with_signed_copy(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path)
    await _quota_event(env, actor)
    dispatcher = NotificationDispatcher(env.db)
    assert await dispatcher.run_once() == 2
    assert await dispatcher.run_once() == 0  # durable cursor: nothing twice
    email = FakeEmail()
    worker = CrewOutboxWorker(CrewStore(env.db), {KIND_CREW_NOTIFY: notify_handler(env.db, webhooks=sender, email_backend=email)})
    assert (await worker.run_once())["done"] == 2

    [delivery] = [r for r in receiver.requests if json.loads(r.content)["type"] == "crew.notification"]
    body = json.loads(delivery.content)
    assert verify_signature(hook["signing_secret"], delivery.content, delivery.headers["X-Remembra-Signature"])
    assert body["kind"] == "handoff" and body["crew_id"] == CREW_A and body["project_id"] == "yaadbooks"
    expected = "cc-1 stopped (billing_error, reported). Work saved; zone pos reserved for the next pickup."
    assert body["items"][0]["text"] == expected
    assert body["items"][0]["link"].endswith(f"/#/crew?project=yaadbooks&view=feed&seq={body['items'][0]['seq']}")
    assert delivery.headers["X-Remembra-Delivery"] == body["id"]
    [mail] = email.sent
    assert mail.to == "mani@example.com" and expected in mail.html and "yaadbooks" in mail.subject


async def test_channels_follow_crew_settings_and_rules(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path, channels=("email",))
    await _quota_event(env, actor)
    assert await NotificationDispatcher(env.db).run_once() == 1  # webhook not enabled for this crew
    rows = await env.db.fetchall("SELECT payload FROM crew_outbox WHERE kind = ?", (KIND_CREW_NOTIFY,))
    assert [json.loads(r["payload"])["target_kind"] for r in rows] == ["email"]
    async with env.db.transaction():
        await env.db.conn.execute(
            "INSERT INTO crew_notification_rules (user_id, crew_id, kind, channel) VALUES (?, ?, 'handoff', 'none')",
            (OWNER, CREW_A),
        )
    await _quota_event(env, actor)
    assert await NotificationDispatcher(env.db).run_once() == 0


async def test_batching_window_merges_follow_ups(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path, channels=("webhook",))
    dispatcher = NotificationDispatcher(env.db)
    await _quota_event(env, actor)
    await dispatcher.run_once()
    tamper = {"kind": "no_verify", "surface": "pretool"}
    for _ in range(2):
        await env.log.emit(
            crew_id=CREW_A,
            type="guard.tamper_blocked",
            actor=actor,
            payload=tamper,
            summary="tamper",
            refs={"session_id": "cs_a"},
        )
        await dispatcher.run_once()
    rows = await env.db.fetchall("SELECT * FROM crew_outbox WHERE kind = ? ORDER BY created_at", (KIND_CREW_NOTIFY,))
    assert len(rows) == 2
    first, batch = rows
    batch_payload = json.loads(batch["payload"])
    assert batch_payload["batch"] == 1 and [i["kind"] for i in batch_payload["items"]] == ["tamper", "tamper"]
    gap = datetime.fromisoformat(batch["next_attempt_at"].replace("Z", "+00:00")) - datetime.fromisoformat(
        first["created_at"].replace("Z", "+00:00")
    )
    assert timedelta(seconds=119) <= gap <= timedelta(seconds=121)
    worker = CrewOutboxWorker(CrewStore(env.db), {KIND_CREW_NOTIFY: notify_handler(env.db, webhooks=sender)})
    assert (await worker.run_once())["done"] == 1  # the batch waits for its window
    body = receiver.bodies()[0]
    assert body["kind"] == "handoff"


async def test_short_loss_is_held_and_cancelled_on_recovery(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path, channels=("webhook",))
    dispatcher = NotificationDispatcher(env.db)
    system = Actor.system()
    lost = await env.log.emit(
        crew_id=CREW_A,
        type="session.lost",
        actor=system,
        payload={"reason": "lease_expired", "last_signal_age_s": 60},
        summary="cc-1 lost",
        refs={"session_id": "cs_a"},
    )
    await dispatcher.run_once()
    row = (await env.db.fetchall("SELECT * FROM crew_outbox WHERE kind = ?", (KIND_CREW_NOTIFY,)))[0]
    held_until = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
    lost_at = datetime.fromisoformat(lost.envelope["ts"].replace("Z", "+00:00"))
    assert abs((held_until - lost_at).total_seconds() - (LOSS_QUIET_S - 60)) < 2
    assert json.loads(row["payload"])["cancel_if_recovered"] == "cs_a"
    await env.log.emit(
        crew_id=CREW_A,
        type="session.recovered",
        actor=system,
        payload={"from": "lost", "down_s": 120, "claims_retaken": [], "tasks_restored": [], "superseded_report_ids": []},
        summary="cc-1 recovered",
        refs={"session_id": "cs_a"},
    )
    await dispatcher.run_once()
    row = await env.db.fetchone("SELECT * FROM crew_outbox WHERE id = ?", (row["id"],))
    assert row["state"] == "done" and row["result_id"] == "suppressed:recovered"

    # a dead process is not a blip: immediate
    await env.log.emit(
        crew_id=CREW_A,
        type="session.lost",
        actor=system,
        payload={"reason": "process_exited", "last_signal_age_s": 5},
        summary="cc-1 lost",
        refs={"session_id": "cs_a"},
    )
    await dispatcher.run_once()
    pending = await env.db.fetchall("SELECT * FROM crew_outbox WHERE kind = ? AND state = 'pending'", (KIND_CREW_NOTIFY,))
    assert len(pending) == 1 and "cancel_if_recovered" not in json.loads(pending[0]["payload"])


async def test_handler_suppresses_a_held_loss_when_the_session_is_back(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path, channels=("webhook",))
    store = CrewStore(env.db)
    async with env.db.transaction():
        oid = await store.enqueue_outbox(
            CREW_A,
            KIND_CREW_NOTIFY,
            {
                "target_id": hook["id"],
                "user_id": OWNER,
                "crew_id": CREW_A,
                "project_id": "yaadbooks",
                "items": [{"seq": 1, "kind": "handoff", "text": "x", "link": "l"}],
                "cancel_if_recovered": "cs_a",
            },
        )
    worker = CrewOutboxWorker(store, {KIND_CREW_NOTIFY: notify_handler(env.db, webhooks=sender)})
    await worker.run_once()
    assert (await store.get_outbox(oid))["result_id"] == "suppressed:recovered" and receiver.bodies() == []


async def test_failed_webhook_retries_and_unverified_target_fails_permanently(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path, channels=("webhook",))
    receiver.status = 503
    await _quota_event(env, actor)
    await NotificationDispatcher(env.db).run_once()
    store = CrewStore(env.db)
    worker = CrewOutboxWorker(store, {KIND_CREW_NOTIFY: notify_handler(env.db, webhooks=sender)})
    assert (await worker.run_once())["retry"] == 1
    async with env.db.transaction():
        await env.db.conn.execute("UPDATE crew_notify_targets SET verified_at = NULL")
        await env.db.conn.execute(
            "UPDATE crew_outbox SET next_attempt_at = ? WHERE kind = ?", (format_ts(datetime.now(UTC)), KIND_CREW_NOTIFY)
        )
    assert (await worker.run_once())["failed"] == 1


async def test_old_history_is_not_alerted(tmp_path):
    env, actor, receiver, sender, hook, _ = await _setup(tmp_path)
    await _quota_event(env, actor)
    later = datetime.now(UTC) + timedelta(hours=2)
    assert await NotificationDispatcher(env.db).run_once(now=later) == 0


async def test_render_copy_for_each_realtime_kind(tmp_path):
    env = await make_env(tmp_path)
    a = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1")
    await add_session(env.db, CREW_A, "cs_b", callsign="codex-1", agent_id="codex")
    await add_zone(env.db, CREW_A, "zn_pos", "pos")
    lookup = Lookup(env.db)
    actor = a.actor().to_dict()
    col = {
        "collision": {
            "id": "col_1",
            "kind": "exclusive_breach",
            "severity": "high",
            "subject": "src/pos/x.ts",
            "zone_id": "zn_pos",
            "session_a": "cs_b",
            "session_b": "cs_a",
        }
    }
    cases = {
        "collision.detected": (col, "codex-1 changed 1 file in zone pos, held by cc-1 (exclusive_breach, high)."),
        "guard.tamper_blocked": (
            {"kind": "no_verify", "surface": "pretool"},
            "cc-1 tried to skip the git hooks (--no-verify). Blocked.",
        ),
        "guard.bypass_used": ({"code_id": "byp_1", "scope": "zone:pos"}, "Bypass code used by cc-1 (zone:pos)."),
        "githook.missing": ({"hook": "pre-push", "state": "missing"}, "cc-1: git hook pre-push is missing."),
        "zone.change_pending": ({"diff_summary": "removes zone pos"}, "Approve zone change from cc-1: removes zone pos"),
    }
    for etype, (payload, text) in cases.items():
        out = await render({"type": etype, "crew_id": CREW_A, "actor": actor, "refs": {}, "payload": payload}, lookup)
        assert out.startswith(text), (etype, out)


async def test_in_app_list_and_read_cursor(tmp_path):
    env, actor, *_ = await _setup(tmp_path)
    ev = await _quota_event(env, actor)
    crews = await CrewStore(env.db).list_crews_for_user(OWNER, None)
    listed = await list_notifications(env.db, crews, OWNER)
    assert listed["unread"] == 1 and listed["items"][0]["kind"] == "handoff" and listed["items"][0]["read"] is False
    await env.inbox.advance_cursor(CREW_A, OWNER, notify.INAPP_STREAM, ev.seq)
    assert (await list_notifications(env.db, crews, OWNER))["unread"] == 0
    assert (await list_notifications(env.db, crews, "stranger"))["items"][0]["read"] is False


async def test_lifespan_delivers_a_quota_webhook_within_five_seconds(tmp_path):
    """The production chain: startup hooks → bus wakes the dispatcher → outbox worker → signed POST."""
    from remembra.crew.db import CrewDatabase
    from remembra.storage.database import Database
    from tests.crew.test_crew_startup import StubMemoryService
    from tests.crew.wp7_support import seed_crew

    receiver = Receiver()
    sender = WebhookSender(resolver=public_resolver, transport=receiver.transport())

    @asynccontextmanager
    async def lifespan(app):
        app.state.tasks = TaskRegistry()
        main = Database(str(tmp_path / "remembra.db"))
        await main.connect()
        await main.init_schema()
        app.state.db = main
        app.state.memory_service = StubMemoryService()
        db = CrewDatabase(str(tmp_path / "crew.db"))
        await db.init_schema()
        await seed_crew(db, CREW_A)
        app.state.crew_db = db
        app.state.crew_webhook_sender = sender
        yield
        await app.state.tasks.shutdown(timeout=2.0)
        await db.close()
        await main.close()

    app = FastAPI(lifespan=lifespan)
    startup.register(app)
    with TestClient(app) as client:
        db = app.state.crew_db

        async def scenario():
            env_actor = await add_session(db, CREW_A, "cs_a", callsign="cc-1")
            await set_realtime(db, CREW_A, ["webhook"])
            await add_zone(db, CREW_A, "zn_pos", "pos")
            await add_claim(db, CREW_A, "clm_pos", "zn_pos", "cs_a", state="reserved")
            await NotifyTargets(db, webhooks=sender).add(OWNER, "webhook", HOOK_URL)
            started = asyncio.get_running_loop().time()
            await app.state.crew_events.emit(
                crew_id=CREW_A,
                type="session.quota_blocked",
                actor=env_actor.actor(),
                payload={"error": "billing_error", "source": "reported", "baton_ref": None, "claims_reserved": ["clm_pos"]},
                summary="cc-1 quota",
                refs={"session_id": "cs_a"},
            )
            while not receiver.bodies():
                assert asyncio.get_running_loop().time() - started < 5.0, "no webhook within 5 s"
                await asyncio.sleep(0.05)
            return receiver.bodies()[0]

        body = client.portal.call(scenario)
        assert body["items"][0]["text"].startswith("cc-1 stopped (billing_error, reported)")
        assert app.state.crew_notifier is not None
    assert app.state.crew_notifier is None


def test_deep_links_use_the_dashboard_crew_route(monkeypatch):
    """§9.12: the link opens the exact item. The format is the one ``lib/nav.ts`` parseHash and
    ``lib/crew/routes.ts`` parseCrewRoute read (the same literals are asserted in routes.test.ts)."""
    monkeypatch.setenv(notify.DASHBOARD_URL_ENV, "https://app.remembra.dev/")
    assert (
        notify.deep_link("yaadbooks", 1042, "handoff") == "https://app.remembra.dev/#/crew?project=yaadbooks&view=feed&seq=1042"
    )
    assert (
        notify.deep_link("yaadbooks", 7, "zone_change") == "https://app.remembra.dev/#/crew?project=yaadbooks&view=policy&seq=7"
    )
    assert notify.deep_link("yaadbooks", 8, "decision") == "https://app.remembra.dev/#/crew?project=yaadbooks&view=channel&seq=8"
    assert (
        notify.deep_link("yaadbooks", 9, "task_done", {"report_id": "rpt_0123456789abcdef"})
        == "https://app.remembra.dev/#/crew?project=yaadbooks&view=report&report=rpt_0123456789abcdef&seq=9"
    )
    assert notify.deep_link("yaadbooks", 9, "task_done", {}).endswith("#/crew?project=yaadbooks&view=feed&seq=9")
    assert notify.deep_link("my proj/x", 3).endswith("#/crew?project=my+proj%2Fx&view=feed&seq=3")
