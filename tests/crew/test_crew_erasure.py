"""R-23 with Crew mode: account erasure covers ``crew.db`` (remembra.crew.erasure).

* Coverage: the real migrated crew schema is fully classified. Every table has
  a rule or an exemption and every user-keyed or actor column is matched, so a
  crew table added later fails here until it has a rule.
* Behaviour: every crew table gets rows for three contexts: the victim's own
  crew, the victim working in a bystander's crew, and the bystander in their
  own crew. Erasing the victim removes the victim's crew entirely and every
  cell holding the victim's user id, session ids or message ids (except the
  bystander's hash-chained event log, which keeps opaque session ids of its
  own crew), and keeps every bystander row.
* Wiring: with Crew mode on, the booted app's eraser covers ``crew.db`` and
  a real erase through it removes the victim's crew rows; shutdown detaches it.
* Real chains: two users' crews written by the real services (the victim also
  works in the bystander's crew, as a session and as a human). Erasing the
  victim leaves no id of it anywhere and its text only in shared history (a
  decision in force, unattributed); the bystander's hash-chained log keeps every
  seq and link, the victim's events in it are tombstoned, and verify_crew_chain
  passes. A tombstone cannot hide an edit, and retention prunes it with its event.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from remembra.account.erasure import AccountEraser, registry_problems
from remembra.crew.db import CrewDatabase
from remembra.crew.erasure import CREW_ERASURE_RULES, CREW_EXEMPT_TABLES, crew_extra_database
from remembra.storage.database import Database

VICTIM, BYSTANDER = "u_victim", "u_bystander"
CREW_V, CREW_B = "crw_victim0000000000", "crw_bystander000000"
# (context name, crew, user, session, message)
# The bystander's rows go in before the victim's guest rows: a table keyed by the crew alone
# (crew_repo_trees, crew_zone_files) holds one row per crew, and that one is the bystander's.
CONTEXTS = (
    ("own", CREW_V, VICTIM, "cs_victim_own", "msg_victim_own"),
    ("bystander", CREW_B, BYSTANDER, "cs_bystander", "msg_bystander"),
    ("guest", CREW_B, VICTIM, "cs_victim_guest", "msg_victim_guest"),
)
VICTIM_VALUES = {VICTIM, "cs_victim_own", "cs_victim_guest", "msg_victim_own", "msg_victim_guest", CREW_V}

_USER_COLUMNS = {
    "user_id",
    "owner_user_id",
    "holder_user_id",
    "author_user_id",
    "uploaded_by_user",
    "created_by",
    "added_by",
    "issued_by",
    "confirmed_by",
    "decided_by",
    "reviewed_by",
    "resolved_by",
    "frozen_by",
    "shared_by",
    "uploaded_by",
}
_SESSION_COLUMNS = {
    "session_id",
    "holder_session_id",
    "author_session_id",
    "owner_session_id",
    "uploaded_by_session",
    "from_session",
    "to_session",
    "session_a",
    "session_b",
    "voter_id",
    "principal",
    "recipient",
    "claimed_by",
}
_CHECK_IN = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


async def _open(tmp_path: Path) -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    return db


async def _schema(db: CrewDatabase) -> dict[str, list[dict[str, Any]]]:
    cursor = await db.conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
    out: dict[str, list[dict[str, Any]]] = {}
    for name, sql in await cursor.fetchall():
        info = await db.conn.execute(f'PRAGMA table_info("{name}")')
        checks = {m.group(1): [v.strip().strip("'") for v in m.group(2).split(",")] for m in _CHECK_IN.finditer(sql or "")}
        out[str(name)] = [
            {"name": r[1], "type": (r[2] or "").upper(), "notnull": r[3], "pk": r[5], "choices": checks.get(r[1])}
            for r in await info.fetchall()
        ]
    return out


def _value(table: str, col: dict[str, Any], ctx: tuple[str, str, str, str, str], n: int) -> Any:
    label, crew, user, session, message = ctx
    name = col["name"]
    if col["choices"]:
        return col["choices"][0]
    if table == "crews" and name == "id":
        return crew
    if table == "crew_sessions" and name == "id":
        return session
    if table == "crew_messages" and name == "id":
        return message
    if name == "crew_id":
        return crew
    if name in ("message_id", "reply_to_id", "thread_root_id") and not (table == "crew_messages" and label != "bystander"):
        return message if table != "crew_messages" else "msg_victim_guest"  # the bystander replies to the victim
    if table == "crew_events" and name == "owner_user_id":
        return VICTIM if crew == CREW_V else BYSTANDER  # events are keyed by the crew's owner
    if name in _USER_COLUMNS:
        return user
    if name in _SESSION_COLUMNS:
        return session
    if name in ("created_at", "updated_at", "joined_at", "added_at", "registered_at", "ts", "first_at", "last_at"):
        return "2026-09-20T10:00:00.000Z"
    if name in ("seq", "number", "first_seq", "last_seq", "version", "v", "epoch", "position", "priority"):
        return n
    if "INT" in col["type"]:
        return 0
    if "REAL" in col["type"]:
        return 0.5
    return f"{table}:{name}:{label}"


async def _seed(db: CrewDatabase) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every crew table gets one row per context; returns what was written."""
    schema = await _schema(db)
    written: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for n, ctx in enumerate(CONTEXTS, start=1):
        for table, cols in schema.items():
            if table in CREW_EXEMPT_TABLES:
                continue
            if table == "crews" and ctx[0] == "guest":
                continue  # the victim does not own the bystander's crew
            row = {c["name"]: _value(table, c, ctx, n) for c in cols}
            names = ", ".join(f'"{k}"' for k in row)
            marks = ", ".join("?" for _ in row)
            cursor = await db.conn.execute(f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({marks})', list(row.values()))
            if cursor.rowcount:
                written.setdefault(table, []).append((ctx[0], row))
            else:
                assert ctx[0] == "guest", (table, ctx[0])  # only a one-row-per-crew table refuses the guest row
    await db.conn.commit()
    return written


async def _rows(db: CrewDatabase) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for table in await _schema(db):
        cursor = await db.conn.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cursor.description]
        out[table] = [dict(zip(cols, r, strict=True)) for r in await cursor.fetchall()]
    return out


async def test_every_crew_table_is_covered_by_the_rules(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        schema = {t: [c["name"] for c in cols] for t, cols in (await _schema(db)).items()}
    finally:
        await db.close()
    assert registry_problems(schema, CREW_ERASURE_RULES, CREW_EXEMPT_TABLES, ("sqlite_",)) == []
    assert {rule.table for rule in CREW_ERASURE_RULES} - set(schema) == set()
    assert set(CREW_EXEMPT_TABLES) - set(schema) <= {"sqlite_sequence", "sqlite_stat1"}


async def test_erasure_removes_the_victims_crew_rows_and_keeps_the_bystanders(tmp_path: Path) -> None:
    crew = await _open(tmp_path)
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        written = await _seed(crew)
        assert len(written) == len(await _schema(crew)) - len(set(CREW_EXEMPT_TABLES) & set(await _schema(crew)))
        eraser = AccountEraser(main, None, extra_databases=[crew_extra_database(crew)])
        receipt = await eraser.erase(VICTIM)
        after = await _rows(crew)

        assert receipt.unregistered_tables == []
        assert receipt.rows.get("crew:crews") == 1 and receipt.rows.get("crew:crew_sessions") == 2
        for table, rows in after.items():
            for row in rows:
                assert row.get("crew_id") != CREW_V, (table, row)
                leaked = {k: v for k, v in row.items() if v in VICTIM_VALUES}
                if table == "crew_events":
                    # The bystander's event log is their record: it may keep the opaque id of the
                    # victim's session in their crew, never the victim's user id.
                    assert set(leaked) <= {"session_id", "actor_id", "ref_id"} and VICTIM not in leaked.values(), row
                    continue
                assert leaked == {}, (table, row)
        for table, rows in written.items():
            kept = [r for label, r in rows if label == "bystander"]
            for row in kept:
                key = {k: row[k] for k in ("id", "crew_id", "seq", "user_id", "key", "principal") if k in row}
                assert any(all(a.get(k) == v for k, v in key.items()) for a in after[table]), (table, key)
        # The victim's guest work in the bystander's crew is gone; the bystander's task it created stays, unattributed.
        tasks = {r["id"]: r for r in after["crew_tasks"]}
        assert "crew_tasks:id:guest" in tasks and tasks["crew_tasks:id:guest"]["created_by"] is None
        assert tasks["crew_tasks:id:guest"]["owner_user_id"] is None and tasks["crew_tasks:id:guest"]["owner_session_id"] is None
        assert [r["id"] for r in after["crew_sessions"]] == ["cs_bystander"]
        assert [r["id"] for r in after["crew_messages"]] == ["msg_bystander"]
        assert [r["id"] for r in after["crews"]] == [CREW_B]
    finally:
        await crew.close()
        await main.close()


async def test_a_second_erase_finds_nothing(tmp_path: Path) -> None:
    crew = await _open(tmp_path)
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        await _seed(crew)
        eraser = AccountEraser(main, None, extra_databases=[crew_extra_database(crew)])
        await eraser.erase(VICTIM)
        again = await eraser.erase(VICTIM)
        assert {k: v for k, v in again.rows.items() if k.startswith("crew:")} == {}
    finally:
        await crew.close()
        await main.close()


def test_eraser_attach_replaces_by_name_and_detach_removes() -> None:
    eraser = AccountEraser(object(), None)
    first, second = crew_extra_database(object()), crew_extra_database(object())
    eraser.attach(first)
    eraser.attach(second)
    assert eraser.databases == ["crew"] and eraser._extra == [second]
    eraser.detach("crew")
    eraser.detach("crew")
    assert eraser.databases == []
    with pytest.raises(TypeError):
        eraser.attach(object())  # type: ignore[arg-type]


async def test_booted_app_with_crew_mode_erases_crew_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qdrant_client import AsyncQdrantClient

    import remembra.config
    import remembra.main as main
    from remembra.storage.qdrant import QdrantStore

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "crew_erasure_boot_test")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_PRE_MIGRATION_BACKUP", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_CREW_MODE", "true")
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self: QdrantStore) -> AsyncQdrantClient:
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    app = main.create_app()
    async with app.router.lifespan_context(app):
        eraser = app.state.account_eraser
        assert eraser.databases == ["crew"]
        crew = app.state.crew_db
        await _seed(crew)
        receipt = await eraser.erase(VICTIM)
        assert receipt.rows.get("crew:crews") == 1
        cursor = await crew.conn.execute("SELECT COUNT(*) FROM crew_sessions WHERE user_id = ?", (VICTIM,))
        assert (await cursor.fetchone())[0] == 0
        cursor = await crew.conn.execute("SELECT COUNT(*) FROM crew_sessions WHERE user_id = ?", (BYSTANDER,))
        assert (await cursor.fetchone())[0] == 1
    assert eraser.databases == []
    monkeypatch.setattr(remembra.config, "_settings", None)


# ---------------------------------------------------------------------------
# Real crews, real hash chains: two users, one erased
# ---------------------------------------------------------------------------

SECRET = "VICTIM-SECRET"
KEEP = "BYSTANDER-KEEP"


async def _real_crews(tmp_path: Path) -> dict[str, Any]:
    """The bystander's crew (where the victim works as a member, through its own session and as a human)
    and the victim's own crew, every row written by the real services and the real event log."""
    from remembra.crew.channel import CrewChannel
    from remembra.crew.checkpoints import CheckpointService
    from remembra.crew.decisions import CrewDecisions, CrewRef
    from remembra.crew.inbox import Author, CrewInbox
    from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS
    from remembra.crew.store import crew_id_for
    from remembra.crew.tasks import Caller
    from tests.crew.sessions_support import join_req, make_env
    from tests.crew.wp6_support import seed_session

    env = await make_env(tmp_path)
    by = await env.svc.join(user_id=BYSTANDER, req=join_req("s-b"), host_token=None, session_token=None)
    mine = await env.svc.join(user_id=VICTIM, req=join_req("s-v", project="victimproj"), host_token=None, session_token=None)
    crew_b = crew_id_for(BYSTANDER, "yaadbooks")
    crew_v = crew_id_for(VICTIM, "victimproj")
    async with env.db.transaction():
        await env.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_by, added_at) VALUES (?, ?, 'member', ?, ?)",
            (crew_b, VICTIM, BYSTANDER, "2026-09-26T08:00:00.000Z"),
        )
    guest = await seed_session(env.db, crew_b, callsign="cc-9", user_id=VICTIM)
    by_row = by.session

    async def limits(_owner: str):  # noqa: ANN202
        return SELF_HOSTED_CREW_LIMITS

    checkpoints = CheckpointService(env.log, limits_resolver=limits)
    inbox = CrewInbox(env.log)
    decisions = CrewDecisions(env.log, inbox)
    channel = CrewChannel(env.log, inbox=inbox, decisions=decisions)
    ref_b = CrewRef(crew_b, BYSTANDER, "yaadbooks")
    ref_v = CrewRef(crew_v, VICTIM, "victimproj")

    # the victim's work in the bystander's crew: its session and its dashboard login
    await checkpoints.ingest(
        crew_b, Caller.for_session(guest), {"session_id": guest["id"], "trigger": "turn", "facts": {"summary": f"{SECRET} plan"}}
    )
    guest_msg = await channel.post(ref_b, Author.session(guest), kind="note", body=f"{SECRET} session note", client_msg_id="g1")
    human_msg = await channel.post(ref_b, Author.human(VICTIM), kind="chat", body=f"{SECRET} human chat", client_msg_id="h1")
    proposed = await decisions.create(ref_b, Author.session(guest), title=f"{SECRET} proposal", decision="Use floats")
    adopted = await decisions.create(ref_b, Author.human(VICTIM), title=f"{SECRET} rule", decision="Round half-up")
    # the bystander's own work, one reply to the victim among it
    await checkpoints.ingest(
        crew_b, Caller.for_session(by_row), {"session_id": by_row["id"], "trigger": "turn", "facts": {"summary": f"{KEEP} plan"}}
    )
    by_msg = await channel.post(ref_b, Author.session(by_row), kind="note", body=f"{KEEP} note", client_msg_id="b1")
    reply = await channel.post(
        ref_b,
        Author.session(by_row),
        kind="chat",
        body=f"{KEEP} reply",
        client_msg_id="b2",
        reply_to_id=guest_msg.message["id"],
    )
    # the victim's own crew
    await channel.post(ref_v, Author.session(mine.session), kind="note", body=f"{SECRET} own crew", client_msg_id="v1")
    return {
        "env": env,
        "crew_b": crew_b,
        "crew_v": crew_v,
        "guest": guest,
        "victim_session": mine.session,
        "bystander_session": by_row,
        "victim_messages": [guest_msg.message["id"], human_msg.message["id"]],
        "bystander_messages": [by_msg.message["id"], reply.message["id"]],
        "proposed": proposed["id"],
        "adopted": adopted["id"],
    }


async def _event_rows(db: Any, crew_id: str) -> list[dict[str, Any]]:
    return await db.fetchall("SELECT * FROM crew_events WHERE crew_id = ? ORDER BY seq", (crew_id,))


async def test_erasure_tombstones_the_victims_events_and_the_chain_still_verifies(tmp_path: Path) -> None:
    from remembra.crew.events import TOMBSTONE_SUMMARY, verify_crew_chain

    world = await _real_crews(tmp_path)
    env, crew_b, crew_v = world["env"], world["crew_b"], world["crew_v"]
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        assert (await verify_crew_chain(env.conn, crew_b)).ok
        before = await _event_rows(env.db, crew_b)
        head = await env.one("SELECT last_seq, last_hash FROM crews WHERE id = ?", (crew_b,))
        victim_ids = {VICTIM, world["guest"]["id"], world["victim_session"]["id"], *world["victim_messages"]}
        # the victim is in the bystander's log before erasure: as actor, in refs and in payloads
        blob = json.dumps(before)
        assert SECRET in blob and all(v in blob for v in victim_ids - {world["victim_session"]["id"]})

        receipt = await AccountEraser(main, None, extra_databases=[crew_extra_database(env.db)]).erase(VICTIM)
        tombstoned = receipt.rows.get("crew:crew_events_tombstoned", 0)
        assert tombstoned > 0

        # the bystander's chain still verifies end to end: same seqs, types and links, same head
        report = await verify_crew_chain(env.conn, crew_b)
        assert report.ok, report.errors
        assert report.tombstoned == tombstoned and report.checked == len(before)
        after = await _event_rows(env.db, crew_b)
        assert [(r["seq"], r["type"], r["prev_hash"], r["hash"]) for r in after] == [
            (r["seq"], r["type"], r["prev_hash"], r["hash"]) for r in before
        ]
        assert await env.one("SELECT last_seq, last_hash FROM crews WHERE id = ?", (crew_b,)) == head
        # nothing of the victim is left in it; events without the victim are unchanged, byte for byte
        for old, new in zip(before, after, strict=True):
            text = json.dumps(new)
            assert SECRET not in text and not any(v in text for v in victim_ids), new
            if new["summary"] == TOMBSTONE_SUMMARY:
                assert json.loads(new["payload"]) == {"erased": True} and json.loads(new["refs"]) == {}
            else:
                assert new == old
        kept = [r for r in after if r["summary"] != TOMBSTONE_SUMMARY]
        assert any(KEEP in json.dumps(r) for r in kept)  # the bystander's own events keep their content

        # the victim's own crew is gone whole
        for table in ("crews", "crew_events", "crew_sessions", "crew_messages"):
            where = "id" if table == "crews" else "crew_id"
            assert await env.all(f"SELECT 1 FROM {table} WHERE {where} = ?", (crew_v,)) == []

        # across every table: no victim id anywhere, and its text survives only as shared crew history
        dump = {t: await env.all(f'SELECT * FROM "{t}"') for t in await _table_names(env.db)}
        for table, rows in dump.items():
            for row in rows:
                text = json.dumps(row)
                assert not any(v in text for v in victim_ids), (table, row)
                if SECRET in text:
                    # the one kept row: the decision in force the victim made, the owner's rule now, unattributed
                    assert table == "crew_decisions" and row["id"] == world["adopted"], (table, row)
                    assert row["decided_by"] is None and row["confirmed_by"] is None and row["participants"] is None
        assert world["proposed"] not in {r["id"] for r in dump["crew_decisions"]}  # nobody adopted it: the victim's own
        # the bystander's rows are all there
        assert {r["id"] for r in dump["crew_messages"]} == set(world["bystander_messages"])
        assert [r["reply_to_id"] for r in dump["crew_messages"] if r["id"] == world["bystander_messages"][1]] == [None]
        assert world["bystander_session"]["id"] in {r["id"] for r in dump["crew_sessions"]}
        assert any(KEEP in json.dumps(r) for r in dump["crew_checkpoints"])
        assert VICTIM not in {r["user_id"] for r in dump["crew_members"]}

        # idempotent: a second run finds nothing, and the chain still verifies
        again = await AccountEraser(main, None, extra_databases=[crew_extra_database(env.db)]).erase(VICTIM)
        assert {k: v for k, v in again.rows.items() if k.startswith("crew:")} == {}
        assert (await verify_crew_chain(env.conn, crew_b)).ok
    finally:
        await env.db.close()
        await main.close()


async def test_a_tombstone_cannot_hide_an_edit(tmp_path: Path) -> None:
    from remembra.crew.events import verify_crew_chain

    world = await _real_crews(tmp_path)
    env, crew_b = world["env"], world["crew_b"]
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        await AccountEraser(main, None, extra_databases=[crew_extra_database(env.db)]).erase(VICTIM)
        tomb = await env.one("SELECT seq FROM crew_event_tombstones WHERE crew_id = ? ORDER BY seq LIMIT 1", (crew_b,))
        assert tomb is not None
        # content written back into a tombstoned event is caught
        async with env.db.transaction():
            await env.conn.execute(
                'UPDATE crew_events SET payload = \'{"note":"forged"}\' WHERE crew_id = ? AND seq = ?', (crew_b, tomb["seq"])
            )
        report = await verify_crew_chain(env.conn, crew_b)
        assert f"seq {tomb['seq']}: tombstoned event carries content" in report.errors
        # so is a tombstone whose links were changed
        async with env.db.transaction():
            await env.conn.execute(
                "UPDATE crew_events SET payload = '{\"erased\":true}' WHERE crew_id = ? AND seq = ?", (crew_b, tomb["seq"])
            )
            await env.conn.execute(
                "UPDATE crew_event_tombstones SET hash = 'f00' WHERE crew_id = ? AND seq = ?", (crew_b, tomb["seq"])
            )
        report = await verify_crew_chain(env.conn, crew_b)
        assert any("links differ" in e for e in report.errors), report.errors
        # and an edit of an event that is not tombstoned is still a hash mismatch
        async with env.db.transaction():
            await env.conn.execute(
                "UPDATE crew_event_tombstones SET hash = (SELECT hash FROM crew_events WHERE crew_id = ? AND seq = ?)"
                " WHERE crew_id = ? AND seq = ?",
                (crew_b, tomb["seq"], crew_b, tomb["seq"]),
            )
            live = await env.one(
                "SELECT seq FROM crew_events WHERE crew_id = ? AND seq NOT IN (SELECT seq FROM crew_event_tombstones"
                " WHERE crew_id = ?) ORDER BY seq DESC LIMIT 1",
                (crew_b, crew_b),
            )
            await env.conn.execute(
                "UPDATE crew_events SET summary = 'edited' WHERE crew_id = ? AND seq = ?", (crew_b, live["seq"])
            )
        report = await verify_crew_chain(env.conn, crew_b)
        assert report.errors == [f"seq {live['seq']}: hash mismatch"]
    finally:
        await env.db.close()
        await main.close()


async def test_retention_prunes_a_tombstone_with_its_event(tmp_path: Path) -> None:
    from datetime import timedelta

    from remembra.crew.events import utc_now, verify_crew_chain
    from remembra.crew.retention import RetentionPolicy, prune_crew_events

    world = await _real_crews(tmp_path)
    env, crew_b = world["env"], world["crew_b"]
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        await AccountEraser(main, None, extra_databases=[crew_extra_database(env.db)]).erase(VICTIM)
        policy = RetentionPolicy(
            raw_event_days=0, burst_days=0, checkpoint_facts_days=None, ended_session_days=None, baton_brief_days=None
        )
        pruned = await prune_crew_events(env.db, crew_b, policy, now=utc_now() + timedelta(days=400))
        assert pruned > 0
        orphans = await env.all(
            "SELECT seq FROM crew_event_tombstones t WHERE crew_id = ? AND NOT EXISTS"
            " (SELECT 1 FROM crew_events e WHERE e.crew_id = t.crew_id AND e.seq = t.seq)",
            (crew_b,),
        )
        assert orphans == []
        report = await verify_crew_chain(env.conn, crew_b)
        assert report.ok, report.errors
    finally:
        await env.db.close()
        await main.close()


async def test_tombstones_match_the_email_too(tmp_path: Path) -> None:
    from remembra.crew.erasure import tombstone_account_events
    from remembra.crew.events import CrewEventLog, verify_crew_chain

    world = await _real_crews(tmp_path)
    env, crew_b = world["env"], world["crew_b"]
    from remembra.crew.channel import CrewChannel
    from remembra.crew.decisions import CrewRef
    from remembra.crew.inbox import Author

    channel = CrewChannel(CrewEventLog(env.db))
    await channel.post(
        CrewRef(crew_b, BYSTANDER, "yaadbooks"),
        Author.human(BYSTANDER),
        kind="chat",
        body="send the alerts to Victim@Example.com",
        client_msg_id="e1",
    )
    posted = await env.all("SELECT seq FROM crew_events WHERE crew_id = ? AND instr(payload, 'Example.com') > 0", (crew_b,))
    assert posted
    counts = await tombstone_account_events(env.conn, "u_nobody", "victim@example.com")
    assert counts == {"crew_events_tombstoned": len(posted)}
    assert (
        await env.all("SELECT seq FROM crew_events WHERE crew_id = ? AND instr(LOWER(payload), 'victim@') > 0", (crew_b,)) == []
    )
    assert (await verify_crew_chain(env.conn, crew_b)).ok
    await env.db.close()


async def _table_names(db: Any) -> list[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return [r["name"] for r in rows]


def test_every_crew_table_says_what_it_holds_per_user() -> None:
    from remembra.crew.db import CREW_TABLES
    from remembra.crew.erasure import CREW_TABLE_HOLDINGS

    assert set(CREW_TABLE_HOLDINGS) == set(CREW_TABLES)
    for table, text in CREW_TABLE_HOLDINGS.items():
        assert text.split(":", 1)[0] in ("own", "shared", "crew-only"), table
