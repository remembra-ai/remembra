"""WP-16: the ``REMEMBRA_CREW_MODE`` flag through the real app, and the rollback it promises.

The lifecycle test boots ``remembra.main.create_app()`` with its real lifespan three times over one
volume (main database + ``crew.db`` next to it, in-memory Qdrant, a fake embeddings endpoint):

1. flag on: crew routes answer, ``/health/ready`` reports ``crew`` ok at the latest schema, a crew
   is stored in ``crew.db``;
2. flag off (the rollback): crew routes are gone, readiness says ``disabled`` without degrading the
   instance, and ``crew.db`` is not opened, written or migrated (same bytes, same mtime);
3. flag on again: the crew from step 1 is still there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

import remembra.config as config_module
import remembra.main as main_module
from remembra.api.v1 import websocket
from remembra.config import Settings
from remembra.core.readiness import DEGRADED, OK, ReadinessChecker
from remembra.crew import startup
from remembra.crew.db import CREW_MIGRATION_RUNNER, CrewDatabase
from remembra.storage.qdrant import QdrantStore
from tests.crew import wp8_seed as seed
from tests.test_rel_background import DIMS, Provider


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(volume: Path) -> list[str]:
    """The volume's files, less the main database's pre-migration backups (written on the
    first boot that finds tables in it; they never hold crew.db)."""
    return sorted(p.name for p in volume.iterdir() if p.name != "backups")


@pytest.fixture()
def volume(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{data / 'remembra.db'}")
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_EMBEDDING_DIMENSIONS", str(DIMS))
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "wp16_flag")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_AUTH_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_TEMPORAL_CLEANUP_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_RECONCILE_INTERVAL_HOURS", "0")
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self):  # noqa: ANN001
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    return data


async def _boot(monkeypatch, crew_mode: str, probe):  # noqa: ANN001
    """One process lifetime: create_app with the flag, run the lifespan, call ``probe(app, client)``."""
    monkeypatch.setenv(main_module.CREW_MODE_ENV, crew_mode)
    monkeypatch.setattr(config_module, "_settings", None)
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        app.state.embeddings._get_embedder()._client = httpx.AsyncClient(transport=Provider().transport())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await probe(app, client)


async def test_flag_on_off_on_keeps_crew_db_intact_and_readiness_tells_the_truth(volume, monkeypatch) -> None:
    crew_file = volume / "crew.db"

    async def on_first(app, client):  # noqa: ANN001
        assert app.state.crew_db is not None and Path(app.state.crew_db.db_path) == crew_file
        ready = (await client.get("/health/ready")).json()
        assert ready["components"]["crew"] == {
            "status": OK,
            "enabled": True,
            "schema_version": CREW_MIGRATION_RUNNER.latest_version,
            "latest_version": CREW_MIGRATION_RUNNER.latest_version,
        }
        assert "crew" not in ready["degraded_components"]
        crew_id = await seed.crew(app.state.crew_db, "default_user", "yaadbooks")
        listed = (await client.get("/api/v1/crews")).json()
        assert [c["crew"]["id"] for c in listed["crews"]] == [crew_id]
        return crew_id

    crew_id = await _boot(monkeypatch, "true", on_first)
    assert crew_file.exists()
    before = (_sha(crew_file), crew_file.stat().st_mtime_ns, _files(volume))

    async def off(app, client):  # noqa: ANN001
        assert getattr(app.state, "crew_db", None) is None
        assert (await client.get("/api/v1/crews")).status_code == 404
        assert (await client.post("/api/v1/crews/resolve", json={"project_id": "yaadbooks"})).status_code == 404
        ready = (await client.get("/health/ready")).json()
        assert ready["components"]["crew"] == {"status": "disabled", "enabled": False}
        assert "crew" not in ready["degraded_components"]
        # memory keeps working with the flag off
        assert (await client.get("/health")).status_code == 200

    await _boot(monkeypatch, "false", off)
    assert (_sha(crew_file), crew_file.stat().st_mtime_ns, _files(volume)) == before

    async def on_again(app, client):  # noqa: ANN001
        listed = (await client.get("/api/v1/crews")).json()
        return [c["crew"]["id"] for c in listed["crews"]]

    assert await _boot(monkeypatch, "1", on_again) == [crew_id]


async def test_readiness_crew_component_states(tmp_path) -> None:
    settings = Settings(openai_api_key="t")
    assert await ReadinessChecker(settings=settings)._check_crew() == {"status": "disabled", "enabled": False}
    off = SimpleNamespace(crew_registered=False, crew_db=None)
    assert (await ReadinessChecker(settings=settings, app_state=off)._check_crew())["status"] == "disabled"

    starting = SimpleNamespace(crew_registered=True, crew_db=None)
    assert await ReadinessChecker(settings=settings, app_state=starting)._check_crew() == {
        "status": DEGRADED,
        "enabled": True,
        "reason": "not_initialized",
    }

    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    state = SimpleNamespace(crew_registered=True, crew_db=db)
    checker = ReadinessChecker(settings=settings, app_state=state)
    assert (await checker._check_crew())["status"] == OK

    # a crew.db behind the code's migrations (e.g. restored from an older snapshot and not migrated)
    async with db.transaction():
        await db.conn.execute("DELETE FROM schema_version")
    behind = await checker._check_crew()
    assert behind["status"] == DEGRADED and behind["reason"] == "schema_mismatch" and behind["schema_version"] == 0

    await db.close()
    gone = await checker._check_crew()
    assert gone["status"] == DEGRADED and gone["reason"] == "unreachable"
