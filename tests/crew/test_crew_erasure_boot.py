"""R-23 with ``crew.db``: the eraser covers crew.db from the erasure job's first run, whatever the flag says.

* An account already due when the server boots is erased by the loop's first
  pass, before the crew startup hooks run. crew.db is attached by the main
  lifespan before that pass, so the account's crew rows go with it.
* With Crew mode off and a crew.db left on disk, erasure still covers it (opened
  for erasure only: ``app.state.crew_db`` stays unset). With Crew mode off and
  no crew.db, none is created.
* Safety net: while a required database exists but is not attached, every erase
  is deferred and the ``users`` row stays, so the next run retries.
* Replay: an account a release without crew erasure already erased (no ``users``
  row, an ``account_erased`` receipt) is erased from crew.db once crew.db is
  attached. An id with no receipt is never touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from remembra.account.erasure import AccountEraser, ErasureDeferred
from remembra.crew.db import CrewDatabase
from remembra.crew.erasure import crew_account_ids, crew_extra_database
from remembra.storage.database import Database

VICTIM = "u_victim"
KEEPER = "u_keeper"
_NOW = "2026-09-20T10:00:00.000Z"


def _boot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, crew_mode: bool) -> None:
    import remembra.config
    from remembra.storage.qdrant import QdrantStore

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "crew_erasure_boot_order")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_PRE_MIGRATION_BACKUP", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_CREW_MODE", "true" if crew_mode else "false")
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    monkeypatch.setattr(remembra.config, "_settings", None)
    from qdrant_client import AsyncQdrantClient

    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self: QdrantStore) -> AsyncQdrantClient:
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)


async def _boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, crew_mode: bool, hold: float = 0.0) -> dict[str, Any]:
    import remembra.config
    import remembra.main as main

    _boot_env(monkeypatch, tmp_path, crew_mode=crew_mode)
    app = main.create_app()
    seen: dict[str, Any] = {}
    async with app.router.lifespan_context(app):
        # the loop's first pass runs at once; give it the event loop
        for _ in range(50):
            await asyncio.sleep(0.02)
        await asyncio.sleep(hold)
        seen["databases"] = list(app.state.account_eraser.databases)
        seen["crew_db"] = getattr(app.state, "crew_db", None)
        seen["crew_erasure_db"] = getattr(app.state, "crew_erasure_db", None)
        seen["eraser"] = app.state.account_eraser
    monkeypatch.setattr(remembra.config, "_settings", None)
    return seen


def _add_due_user(main_db: Path, user_id: str) -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    c = sqlite3.connect(main_db)
    c.execute(
        "INSERT INTO users (id, email, password_hash, created_at, is_active, deleted_at) VALUES (?,?,?,?,0,?)",
        (user_id, f"{user_id}@example.com", "x", old, old),
    )
    c.commit()
    c.close()


def _add_crew_rows(crew_path: Path, user_id: str) -> None:
    k = sqlite3.connect(crew_path)
    k.execute(
        "INSERT INTO crews (id, owner_user_id, project_id, name, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (f"crw_{user_id}", user_id, "secret-project", "crew", _NOW, _NOW),
    )
    k.execute(
        "INSERT INTO crew_hosts (id, user_id, host_label, token_hash, registered_at) VALUES (?,?,?,?,?)",
        (f"hst_{user_id}", user_id, "private-macbook.local", "h", _NOW),
    )
    k.execute(
        "INSERT INTO crew_notify_targets (id, user_id, kind, target, created_at) VALUES (?,?,?,?,?)",
        (f"nt_{user_id}", user_id, "email", "private@example.com", _NOW),
    )
    k.commit()
    k.close()


def _crew_counts(crew_path: Path, user_id: str) -> dict[str, int]:
    k = sqlite3.connect(crew_path)
    try:
        return {
            t: k.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (user_id,)).fetchone()[0]
            for t, col in (("crews", "owner_user_id"), ("crew_hosts", "user_id"), ("crew_notify_targets", "user_id"))
        }
    finally:
        k.close()


def _users(main_db: Path, user_id: str) -> int:
    c = sqlite3.connect(main_db)
    try:
        return int(c.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0])
    finally:
        c.close()


@pytest.mark.parametrize("crew_mode", [True, False])
async def test_an_account_due_at_boot_loses_its_crew_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_mode: bool
) -> None:
    first = await _boot(monkeypatch, tmp_path, crew_mode=True)  # creates boot.db and crew.db
    assert first["databases"] == ["crew"]
    crew_path = tmp_path / "crew.db"
    assert crew_path.exists()
    _add_due_user(tmp_path / "boot.db", VICTIM)
    _add_crew_rows(crew_path, VICTIM)
    _add_crew_rows(crew_path, KEEPER)

    second = await _boot(monkeypatch, tmp_path, crew_mode=crew_mode)

    assert second["databases"] == ["crew"]
    assert _users(tmp_path / "boot.db", VICTIM) == 0
    assert _crew_counts(crew_path, VICTIM) == {"crews": 0, "crew_hosts": 0, "crew_notify_targets": 0}
    assert _crew_counts(crew_path, KEEPER) == {"crews": 1, "crew_hosts": 1, "crew_notify_targets": 1}
    if crew_mode:
        assert second["crew_db"] is not None and second["crew_erasure_db"] is None
    else:
        # opened for erasure only: nothing that keys off app.state.crew_db turns on
        assert second["crew_db"] is None and second["crew_erasure_db"] is not None
    assert second["eraser"].databases == []  # detached and closed at shutdown


async def test_crew_mode_off_without_crew_db_creates_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = await _boot(monkeypatch, tmp_path, crew_mode=False)
    assert seen["databases"] == [] and seen["crew_db"] is None and seen["crew_erasure_db"] is None
    assert not (tmp_path / "crew.db").exists()


async def _main(tmp_path: Path) -> Database:
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    return main


async def _crew(tmp_path: Path) -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    return db


async def test_erase_is_deferred_while_a_required_database_is_not_attached(tmp_path: Path) -> None:
    main = await _main(tmp_path)
    crew = await _crew(tmp_path)
    try:
        _add_due_user(tmp_path / "main.db", VICTIM)
        _add_crew_rows(tmp_path / "crew.db", VICTIM)
        present = {"crew": True}
        eraser = AccountEraser(main, None)
        eraser.require("crew", lambda: present["crew"])
        with pytest.raises(ErasureDeferred):
            await eraser.erase(VICTIM)
        assert await eraser.erase_due(timedelta(days=7)) == []
        assert _users(tmp_path / "main.db", VICTIM) == 1  # kept: the next run retries

        eraser.attach(crew_extra_database(crew))
        receipts = await eraser.erase_due(timedelta(days=7))
        assert [r.user_id for r in receipts] == [VICTIM]
        assert _users(tmp_path / "main.db", VICTIM) == 0
        assert _crew_counts(tmp_path / "crew.db", VICTIM) == {"crews": 0, "crew_hosts": 0, "crew_notify_targets": 0}

        # a required database that does not exist does not block erasure
        other = AccountEraser(main, None)
        other.require("crew", lambda: False)
        _add_due_user(tmp_path / "main.db", KEEPER)
        assert [r.user_id for r in await other.erase_due(timedelta(days=7))] == [KEEPER]
    finally:
        await crew.close()
        await main.close()


async def test_crew_rows_of_an_account_erased_without_crew_db_are_replayed(tmp_path: Path) -> None:
    main = await _main(tmp_path)
    crew = await _crew(tmp_path)
    try:
        _add_due_user(tmp_path / "main.db", VICTIM)
        _add_crew_rows(tmp_path / "crew.db", VICTIM)
        _add_crew_rows(tmp_path / "crew.db", KEEPER)  # KEEPER has no users row and no receipt
        _add_crew_rows(tmp_path / "crew.db", "u_live")
        c = sqlite3.connect(tmp_path / "main.db")
        c.execute(
            "INSERT INTO users (id, email, password_hash, created_at, is_active) VALUES (?,?,?,?,1)",
            ("u_live", "live@example.com", "x", _NOW),
        )
        c.commit()
        c.close()

        # what ce067fd (or a flag-off boot before this fix) did: the main database only
        before = AccountEraser(main, None)
        assert [r.user_id for r in await before.erase_due(timedelta(days=7))] == [VICTIM]
        assert _crew_counts(tmp_path / "crew.db", VICTIM)["crews"] == 1
        assert {VICTIM, KEEPER, "u_live"} <= await crew_account_ids(crew.conn)

        eraser = AccountEraser(main, None, extra_databases=[crew_extra_database(crew)])
        replayed = await eraser.erase_orphans()
        assert [r.user_id for r in replayed] == [VICTIM]
        assert replayed[0].rows.get("crew:crews") == 1
        assert _crew_counts(tmp_path / "crew.db", VICTIM) == {"crews": 0, "crew_hosts": 0, "crew_notify_targets": 0}
        # no receipt, or a live account: never touched
        assert _crew_counts(tmp_path / "crew.db", KEEPER) == {"crews": 1, "crew_hosts": 1, "crew_notify_targets": 1}
        assert _crew_counts(tmp_path / "crew.db", "u_live") == {"crews": 1, "crew_hosts": 1, "crew_notify_targets": 1}
        assert await eraser.erase_orphans() == []

        # erase_due runs the replay first
        _add_crew_rows(tmp_path / "crew.db", VICTIM.replace("victim", "victim2"))
        _add_due_user(tmp_path / "main.db", "u_victim2")
        await AccountEraser(main, None).erase_due(timedelta(days=7))
        assert _crew_counts(tmp_path / "crew.db", "u_victim2")["crews"] == 1
        await eraser.erase_due(timedelta(days=7))
        assert _crew_counts(tmp_path / "crew.db", "u_victim2") == {"crews": 0, "crew_hosts": 0, "crew_notify_targets": 0}
    finally:
        await crew.close()
        await main.close()
