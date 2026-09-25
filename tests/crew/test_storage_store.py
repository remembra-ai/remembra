"""WP-1: crew.db connection/lock isolation and the repository layer (crew/store.py)."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remembra.crew import schemas
from remembra.crew.db import CrewDatabase
from remembra.crew.settings import SettingsError
from remembra.crew.store import (
    ENTITY_TABLES,
    CrewStore,
    CrewStoreError,
    NotFound,
    PreconditionFailed,
    crew_id_for,
    new_id,
    now_iso,
    parse_iso,
)
from remembra.storage.database import Database


@pytest.fixture()
async def crew(tmp_path: Path):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    try:
        yield CrewStore(db)
    finally:
        await db.close()


def committed(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# ids and time
# ---------------------------------------------------------------------------


def test_crew_id_is_deterministic_and_matches_the_contract() -> None:
    cid = crew_id_for("u1", "yaadbooks")
    assert cid == "crw_" + hashlib.sha256(b"u1:yaadbooks").hexdigest()[:16]
    assert cid == crew_id_for("u1", "yaadbooks") != crew_id_for("u2", "yaadbooks")
    assert schemas.is_id("crew", cid)


def test_new_ids_match_the_contract_patterns_and_sort_by_time() -> None:
    for kind in schemas.ID_PREFIXES:
        assert schemas.is_id(kind, new_id(kind)), kind
    assert set(ENTITY_TABLES) | {"event"} == set(schemas.ID_PREFIXES)
    first = new_id("event")
    time.sleep(0.002)
    second = new_id("event")
    assert first < second
    burst = [new_id("claim") for _ in range(2000)]  # many per millisecond
    assert len(set(burst)) == 2000 and burst == sorted(burst)


def test_now_iso_is_server_utc_with_z() -> None:
    stamp = now_iso()
    assert re.fullmatch(schemas.TS_PATTERN, stamp)
    assert abs((parse_iso(stamp) - datetime.now(UTC)).total_seconds()) < 2
    assert now_iso(datetime(2026, 9, 25, 20, 14, 3, 120999, tzinfo=UTC)) == "2026-09-25T20:14:03.120Z"


# ---------------------------------------------------------------------------
# own connection, own lock (D35)
# ---------------------------------------------------------------------------


async def test_crew_writes_do_not_wait_behind_a_main_db_transaction(tmp_path: Path, crew: CrewStore) -> None:
    main = Database(str(tmp_path / "main.db"))
    await main.connect()
    await main.init_schema()
    release = asyncio.Event()
    holding = asyncio.Event()

    async def long_memory_write() -> None:
        async with main.transaction():
            await main.conn.execute("INSERT INTO schema_version VALUES (777, 'hold', 'now')")
            holding.set()
            await release.wait()

    holder = asyncio.create_task(long_memory_write())
    try:
        await holding.wait()
        start = time.monotonic()
        row, created = await asyncio.wait_for(crew.ensure_crew("u1", "yaadbooks"), timeout=2)
        assert created and time.monotonic() - start < 1.0
        assert main.in_transaction  # the main lock was held the whole time
    finally:
        release.set()
        await holder
        await main.close()


async def test_concurrent_crew_transactions_give_a_gap_free_sequence(tmp_path: Path, crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    db = crew.db

    async def emit(i: int) -> int:
        async with db.transaction():
            await db.conn.execute("UPDATE crews SET last_seq = last_seq + 1 WHERE id = ?", (row["id"],))
            got = await db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (row["id"],))
            seq = int(got["last_seq"])
            await asyncio.sleep(0)  # yield inside the transaction: others must still wait
            await db.conn.execute(
                """INSERT INTO crew_events (crew_id, seq, id, owner_user_id, project_id, ts, type, summary, payload)
                   VALUES (?, ?, ?, 'u1', 'p', ?, 'crew.created', 's', '{}')""",
                (row["id"], seq, new_id("event"), now_iso()),
            )
        return seq

    seqs = await asyncio.gather(*(emit(i) for i in range(20)))
    assert sorted(seqs) == list(range(1, 21))
    assert committed(tmp_path / "crew.db", "SELECT seq FROM crew_events ORDER BY seq") == [(i,) for i in range(1, 21)]


async def test_crew_transaction_rolls_back_on_error(tmp_path: Path, crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    with pytest.raises(RuntimeError):
        async with crew.db.transaction():
            await crew.db.conn.execute("UPDATE crews SET last_seq = 99 WHERE id = ?", (row["id"],))
            raise RuntimeError("boom")
    assert committed(tmp_path / "crew.db", "SELECT last_seq FROM crews") == [(0,)]


async def test_not_connected_errors(tmp_path: Path) -> None:
    db = CrewDatabase(str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError):
        _ = db.conn
    with pytest.raises(RuntimeError):
        async with db.transaction():
            pass


# ---------------------------------------------------------------------------
# crews and membership
# ---------------------------------------------------------------------------


async def test_ensure_crew_is_idempotent_and_race_safe(tmp_path: Path, crew: CrewStore) -> None:
    results = await asyncio.gather(*(crew.ensure_crew("u1", "yaadbooks", name="YaadBooks") for _ in range(10)))
    assert sum(created for _, created in results) == 1
    assert {row["id"] for row, _ in results} == {crew_id_for("u1", "yaadbooks")}
    assert committed(tmp_path / "crew.db", "SELECT user_id, role FROM crew_members") == [("u1", "owner")]
    row = results[0][0]
    assert row["settings_version"] == 1 and row["last_seq"] == 0
    settings, version = await crew.get_settings(row["id"])
    assert settings["enforcement"] == "enforce" and version == 1
    assert await crew.get_crew_by_project("u1", "yaadbooks") == await crew.get_crew(row["id"])
    assert await crew.get_crew_by_project("u2", "yaadbooks") is None
    with pytest.raises(ValueError):
        await crew.ensure_crew("", "p")


async def test_list_crews_for_user_respects_membership_and_project_restrictions(crew: CrewStore) -> None:
    a, _ = await crew.ensure_crew("owner", "alpha")
    b, _ = await crew.ensure_crew("owner", "beta")
    other, _ = await crew.ensure_crew("stranger", "gamma")
    await crew.set_member(b["id"], "mate", "member", added_by="owner")

    assert [c["project_id"] for c in await crew.list_crews_for_user("owner")] == ["alpha", "beta"]
    assert [(c["project_id"], c["role"]) for c in await crew.list_crews_for_user("mate")] == [("beta", "member")]
    assert [c["project_id"] for c in await crew.list_crews_for_user("owner", ["beta", "gamma"])] == ["beta"]
    assert await crew.list_crews_for_user("owner", []) == []  # empty allow-list sees nothing
    assert other["id"] not in {c["id"] for c in await crew.list_crews_for_user("owner")}


async def test_membership_roles_and_last_owner_protection(crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("owner", "p")
    cid = row["id"]
    assert await crew.get_member_role(cid, "owner") == "owner"
    await crew.set_member(cid, "ana", "viewer", added_by="owner")
    await crew.set_member(cid, "ana", "admin", added_by="owner")
    assert await crew.get_member_role(cid, "ana") == "admin"
    with pytest.raises(ValueError):
        await crew.set_member(cid, "ana", "god", added_by="owner")
    with pytest.raises(CrewStoreError):
        await crew.set_member(cid, "owner", "member", added_by="owner")
    with pytest.raises(CrewStoreError):
        await crew.remove_member(cid, "owner")
    await crew.set_member(cid, "ana", "owner", added_by="owner")
    assert await crew.remove_member(cid, "owner") is True
    assert await crew.remove_member(cid, "owner") is False
    assert [m["user_id"] for m in await crew.list_members(cid)] == ["ana"]
    with pytest.raises(NotFound):
        await crew.set_member("crw_missing", "x", "member", added_by="owner")


# ---------------------------------------------------------------------------
# settings with If-Match
# ---------------------------------------------------------------------------


async def test_patch_settings_uses_if_match_and_bumps_the_version(tmp_path: Path, crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    cid = row["id"]
    settings, version, changed = await crew.patch_settings(cid, {"enforcement": "observe"}, if_match=1)
    assert (settings["enforcement"], version, changed) == ("observe", 2, ["enforcement"])
    with pytest.raises(PreconditionFailed) as exc:
        await crew.patch_settings(cid, {"enforcement": "enforce"}, if_match=1)
    assert exc.value.current_version == 2
    # No-op patch: nothing written, version unchanged.
    _, version, changed = await crew.patch_settings(cid, {"enforcement": "observe"}, if_match=2)
    assert (version, changed) == (2, [])
    with pytest.raises(SettingsError):
        await crew.patch_settings(cid, {"require_report_for_done": False}, if_match=2)
    stored = committed(tmp_path / "crew.db", "SELECT settings, settings_version FROM crews")
    assert stored[0][1] == 2 and '"enforcement":"observe"' in stored[0][0]
    with pytest.raises(NotFound):
        await crew.patch_settings("crw_nope", {"enforcement": "off"}, if_match=1)


async def test_concurrent_settings_patches_one_wins(crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    results = await asyncio.gather(
        *(crew.patch_settings(row["id"], {"wip_per_session": n}, if_match=1) for n in (2, 3, 4)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, PreconditionFailed) for r in results) == 2
    assert (await crew.get_settings(row["id"]))[1] == 2


# ---------------------------------------------------------------------------
# entity lookup and numbering
# ---------------------------------------------------------------------------


async def _task(crew: CrewStore, crew_id: str, title: str = "t") -> str:
    tid = new_id("task")
    async with crew.db.transaction():
        number = await crew.next_number(crew_id, "crew_tasks")
        now = now_iso()
        await crew.db.conn.execute(
            """INSERT INTO crew_tasks (id, crew_id, number, title, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'backlog', ?, ?)""",
            (tid, crew_id, number, title, now, now),
        )
    return tid


async def test_entity_lookup_checks_kind_prefix_and_crew(crew: CrewStore) -> None:
    mine, _ = await crew.ensure_crew("u1", "p")
    theirs, _ = await crew.ensure_crew("u2", "q")
    tid = await _task(crew, mine["id"])
    assert (await crew.get_entity("task", tid))["crew_id"] == mine["id"]
    assert await crew.get_entity("claim", tid) is None  # a task id is never a claim id
    assert await crew.get_entity("task", "tsk_doesnotexist") is None
    assert await crew.get_entity("task", "'; DROP TABLE crews; --") is None
    assert (await crew.get_entity("crew", mine["id"]))["project_id"] == "p"
    assert (await crew.get_scoped("task", mine["id"], tid))["id"] == tid
    assert await crew.get_scoped("task", theirs["id"], tid) is None
    with pytest.raises(ValueError):
        await crew.get_entity("widget", "x")
    with pytest.raises(ValueError):
        await crew.get_scoped("crew", mine["id"], mine["id"])
    assert set(ENTITY_TABLES.values()) <= set(__import__("remembra.crew.db", fromlist=["CREW_TABLES"]).CREW_TABLES)


async def test_next_number_is_unique_per_crew_under_concurrency(crew: CrewStore) -> None:
    a, _ = await crew.ensure_crew("u1", "a")
    b, _ = await crew.ensure_crew("u1", "b")
    await asyncio.gather(*(_task(crew, a["id"]) for _ in range(20)), *(_task(crew, b["id"]) for _ in range(5)))
    numbers_a = [r["number"] for r in await crew.db.fetchall("SELECT number FROM crew_tasks WHERE crew_id = ?", (a["id"],))]
    numbers_b = [r["number"] for r in await crew.db.fetchall("SELECT number FROM crew_tasks WHERE crew_id = ?", (b["id"],))]
    assert sorted(numbers_a) == list(range(1, 21))
    assert sorted(numbers_b) == list(range(1, 6))
    with pytest.raises(RuntimeError):
        await crew.next_number(a["id"], "crew_tasks")  # outside a transaction
    with pytest.raises(ValueError):
        async with crew.db.transaction():
            await crew.next_number(a["id"], "crew_claims")


# ---------------------------------------------------------------------------
# idempotency (§4.3)
# ---------------------------------------------------------------------------


async def test_idempotency_is_per_principal_and_expires(tmp_path: Path, crew: CrewStore) -> None:
    assert await crew.idempotency_put("cs_a", "k1", {"seq": 7, "claim_id": "clm_1"}) is True
    assert await crew.idempotency_put("cs_a", "k1", {"seq": 8}) is False  # first response wins
    assert await crew.idempotency_get("cs_a", "k1") == {"seq": 7, "claim_id": "clm_1"}
    assert await crew.idempotency_get("cs_b", "k1") is None  # another principal never sees it
    assert await crew.idempotency_put("cs_b", "k1", {"seq": 9}) is True
    assert await crew.idempotency_get("cs_b", "k1") == {"seq": 9}

    old = now_iso(datetime.now(UTC) - timedelta(hours=73))
    await crew.db.conn.execute("UPDATE crew_idempotency SET created_at = ? WHERE principal = 'cs_a'", (old,))
    await crew.db.conn.commit()
    assert await crew.idempotency_get("cs_a", "k1") is None
    assert await crew.purge_idempotency() == 1
    assert committed(tmp_path / "crew.db", "SELECT principal FROM crew_idempotency") == [("cs_b",)]


@pytest.mark.parametrize(
    "response",
    [
        {"session_token": "st_x"},
        {"host": {"host_token": "ht_x"}},
        {"items": [{"token": "t"}]},
        {"bypass_code": "RCB-AAAAA-BBBBB"},
    ],
)
async def test_token_bearing_responses_are_never_cached(tmp_path: Path, crew: CrewStore, response: dict) -> None:
    with pytest.raises(ValueError):
        await crew.idempotency_put("cs_a", "join", response)
    assert committed(tmp_path / "crew.db", "SELECT COUNT(*) FROM crew_idempotency") == [(0,)]


async def test_idempotency_purge_runs_in_batches(crew: CrewStore) -> None:
    old = now_iso(datetime.now(UTC) - timedelta(days=4))
    for i in range(7):
        await crew.idempotency_put("p", f"k{i}", {"i": i})
    await crew.db.conn.execute("UPDATE crew_idempotency SET created_at = ?", (old,))
    await crew.db.conn.commit()
    assert await crew.purge_idempotency(batch=3) == 7


# ---------------------------------------------------------------------------
# outbox rows
# ---------------------------------------------------------------------------


async def test_outbox_enqueue_commits_with_the_crew_transaction(tmp_path: Path, crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    with pytest.raises(RuntimeError):
        async with crew.db.transaction():
            await crew.enqueue_outbox(row["id"], "memory_promotion", {"x": 1})
            raise RuntimeError("state change failed")
    assert committed(tmp_path / "crew.db", "SELECT COUNT(*) FROM crew_outbox") == [(0,)]

    async with crew.db.transaction():
        first = await crew.enqueue_outbox(row["id"], "memory_promotion", {"x": 1}, dedupe_key="ckp_1")
        again = await crew.enqueue_outbox(row["id"], "memory_promotion", {"x": 2}, dedupe_key="ckp_1")
    assert first == again
    assert committed(tmp_path / "crew.db", "SELECT id, payload, state FROM crew_outbox") == [(first, '{"x":1}', "pending")]

    loose = await crew.enqueue_outbox(row["id"], "relay_handoff", {"y": 1})  # outside a transaction: autocommits
    assert loose != first and (await crew.get_outbox(loose))["state"] == "pending"
    with pytest.raises(ValueError):
        await crew.enqueue_outbox(row["id"], "memory_promotion", {"big": "x" * 70000})
    with pytest.raises(ValueError):
        await crew.enqueue_outbox(row["id"], "memory_promotion", ["not", "an", "object"])  # type: ignore[arg-type]


async def test_outbox_state_transitions(crew: CrewStore) -> None:
    row, _ = await crew.ensure_crew("u1", "p")
    a = await crew.enqueue_outbox(row["id"], "k", {"n": 1})
    b = await crew.enqueue_outbox(row["id"], "k", {"n": 2})
    assert [r["id"] for r in await crew.due_outbox()] == [a, b]

    future = now_iso(datetime.now(UTC) + timedelta(minutes=5))
    assert await crew.mark_outbox_retry(b, "Timeout", future) is True
    assert [r["id"] for r in await crew.due_outbox()] == [a]
    assert [r["id"] for r in await crew.due_outbox(now=now_iso(datetime.now(UTC) + timedelta(minutes=6)))] == [a, b]

    assert await crew.mark_outbox_done(a, "mem_1") is True
    assert await crew.mark_outbox_done(a, "mem_2") is False  # only pending items move
    done = await crew.get_outbox(a)
    assert (done["state"], done["result_id"], done["attempts"]) == ("done", "mem_1", 1)

    assert await crew.mark_outbox_failed(b, "x" * 900) is True
    failed = await crew.get_outbox(b)
    assert (failed["state"], failed["attempts"], len(failed["last_error"])) == ("failed", 2, 500)
    assert await crew.outbox_counts() == {"pending": 0, "done": 1, "failed": 1}
    assert await crew.outbox_counts(row["id"]) == {"pending": 0, "done": 1, "failed": 1}
    assert await crew.outbox_counts("crw_other") == {"pending": 0, "done": 0, "failed": 0}

    assert await crew.requeue_outbox(b) is True
    assert (await crew.get_outbox(b))["state"] == "pending"

    old = now_iso(datetime.now(UTC) - timedelta(days=10))
    await crew.db.conn.execute("UPDATE crew_outbox SET updated_at = ? WHERE id = ?", (old, a))
    await crew.db.conn.commit()
    assert await crew.purge_outbox_done(older_than=timedelta(days=7)) == 1
    assert await crew.get_outbox(a) is None and await crew.get_outbox(b) is not None
