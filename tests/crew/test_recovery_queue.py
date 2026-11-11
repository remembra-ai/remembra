"""Recovery regressions from the owner's 0.17.7 operational exercise."""

from datetime import timedelta

import pytest

from remembra.crew import claims as C
from remembra.crew.store import now_iso
from tests.crew.test_crew_claims import env as claims_env, _claim
from tests.crew.wp5_support import inbox


@pytest.fixture
async def queue_env(tmp_path):
    generator = claims_env.__wrapped__(tmp_path)
    try:
        yield await anext(generator)
    finally:
        await generator.aclose()


async def age_wait(db, claim_id):
    async with db.transaction():
        await db.conn.execute(
            "UPDATE crew_claims SET updated_at = ? WHERE id = ?",
            (now_iso(C.utcnow() - timedelta(seconds=C.QUEUE_TTL_S + 1)), claim_id),
        )


async def test_abandoned_wait_cannot_be_promoted_on_blocker_release(queue_env):
    db, ops, _, a, b = queue_env
    held = await _claim(ops, a, db, "pos")
    wait = await _claim(ops, b, db, "pos", wait=True)
    await age_wait(db, wait.claim["id"])
    await C.release_claim(ops, held.claim, a)
    row = await C.get_claim(db.conn, wait.claim["id"])
    assert row["state"] == "expired" and row["end_reason"] == "queue_timeout"
    assert not await inbox(db, kind="claim_granted")


async def test_sweep_expires_wait_while_blocker_stays_live(queue_env):
    db, ops, _, a, b = queue_env
    held = await _claim(ops, a, db, "pos")
    wait = await _claim(ops, b, db, "pos", wait=True)
    await age_wait(db, wait.claim["id"])
    await C.sweep(ops)
    assert (await C.get_claim(db.conn, held.claim["id"]))["state"] == "active"
    assert (await C.get_claim(db.conn, wait.claim["id"]))["state"] == "expired"


async def test_explicit_request_renews_live_wait_without_losing_fifo(queue_env, monkeypatch):
    db, ops, _, a, b = queue_env
    held = await _claim(ops, a, db, "pos")
    wait = await _claim(ops, b, db, "pos", wait=True)
    later = C.utcnow() + timedelta(seconds=C.QUEUE_TTL_S - 5)
    monkeypatch.setattr(C, "utcnow", lambda: later)
    renewed = await _claim(ops, b, db, "pos", wait=True)
    assert renewed.claim["id"] == wait.claim["id"]
    later += timedelta(seconds=10)
    await C.release_claim(ops, held.claim, a)
    assert (await C.get_claim(db.conn, wait.claim["id"]))["state"] == "active"


async def test_request_after_timeout_creates_new_queue_entry(queue_env):
    db, ops, _, a, b = queue_env
    await _claim(ops, a, db, "pos")
    wait = await _claim(ops, b, db, "pos", wait=True)
    await age_wait(db, wait.claim["id"])
    retry = await _claim(ops, b, db, "pos", wait=True)
    assert retry.status == "queued" and retry.claim["id"] != wait.claim["id"]
    assert (await C.get_claim(db.conn, wait.claim["id"]))["state"] == "expired"


async def test_releasing_reserved_claim_resolves_stale_baton_notice(queue_env):
    db, ops, _, a, _ = queue_env
    held = await _claim(ops, a, db, "pos")
    reserved = await C.release_claim(ops, held.claim, a, baton=True)
    assert (await inbox(db, kind="baton_reserved"))[0]["state"] == "open"
    row = await C.get_claim(db.conn, reserved["claim"]["id"])
    await C.release_claim(ops, row, a)
    assert (await inbox(db, kind="baton_reserved"))[0]["state"] == "resolved"
