"""REL-2/4/6/8/10/15/16/19/20: pending-embedding queue + worker, readiness,
metrics, alerts, task registry, TTL cleanup loop, Qdrant boot checks,
serve_spa path traversal, and a real app lifespan boot.

Qdrant is a real in-process qdrant_client local store (``location=":memory:"``),
SQLite is a real file, embedding providers are ``httpx.MockTransport``.
No network, no paid calls.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

from remembra.config import Settings
from remembra.core.alerts import AlertNotifier
from remembra.core.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    register_state_listener,
    unregister_state_listener,
)
from remembra.core.metrics import REGISTRY
from remembra.core.provider_errors import ProviderErrorKind
from remembra.core.readiness import ReadinessChecker
from remembra.core.tasks import TaskRegistry
from remembra.core.time import utcnow
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingProviderError, EmbeddingService
from remembra.storage.pending_embeddings import PendingEmbeddingQueue, PendingEmbeddingWorker
from remembra.storage.qdrant import QdrantStore

DIMS = 4
QUOTA_BODY = {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Provider:
    """Controllable fake OpenAI embeddings endpoint."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.mode == "quota":
                return httpx.Response(429, json=QUOTA_BODY)
            if self.mode == "down":
                return httpx.Response(503, json={"error": "overloaded"})
            import json

            texts = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": [0.1 * (i + 1)] * DIMS} for i, _ in enumerate(texts)]},
            )

        return httpx.MockTransport(handler)


@pytest.fixture()
async def stack(tmp_path):
    settings = Settings(
        openai_api_key="t",
        embedding_dimensions=DIMS,
        qdrant_collection="rel_test",
        embedding_breaker_failure_threshold=2,
    )
    db = Database(str(tmp_path / "bg.db"))
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    qdrant._client = AsyncQdrantClient(location=":memory:")
    await qdrant.init_collection()
    provider = Provider()
    embeddings = EmbeddingService(settings)
    embeddings._get_embedder()._client = httpx.AsyncClient(transport=provider.transport())  # type: ignore[attr-defined]
    clock = FakeClock()
    embeddings.breaker._clock = clock
    yield {"settings": settings, "db": db, "qdrant": qdrant, "embeddings": embeddings, "provider": provider, "clock": clock}
    await qdrant.close()
    await db.close()


async def _save(db: Database, memory_id: str, content: str = "Mani prefers dark roast coffee", **kw) -> None:
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=kw.get("user_id", "u1"),
        project_id=kw.get("project_id", "default"),
        content=content,
        extracted_facts=[content],
        metadata=kw.get("metadata", {"k": "v"}),
        created_at=utcnow(),
        expires_at=kw.get("expires_at"),
        memory_type=kw.get("memory_type"),
    )


# ---------------------------------------------------------------------------
# Queue semantics
# ---------------------------------------------------------------------------


async def test_queue_lifecycle_backoff_and_dead_letter(stack) -> None:
    db = stack["db"]
    now = [utcnow()]
    q = PendingEmbeddingQueue(db, max_attempts=3, base_backoff_seconds=10, lease_seconds=60, clock=lambda: now[0])

    await q.enqueue("m1", "u1", reason="provider_down")
    await q.enqueue("m1", "u1", reason="provider_down")  # idempotent
    assert (await q.stats())["pending"] == 1

    claimed = await q.claim(10)
    assert [c.memory_id for c in claimed] == ["m1"]
    assert await q.claim(10) == []  # leased

    assert await q.mark_failed("m1", "boom", "vector_store") == "pending"
    item = await q.get("m1")
    assert item and item.attempts == 1 and item.status == "pending"
    assert await q.claim(10) == []  # backing off (10s)
    now[0] += timedelta(seconds=11)
    assert len(await q.claim(10)) == 1
    assert await q.mark_failed("m1", "boom") == "pending"  # attempts=2, backoff 20s
    now[0] += timedelta(seconds=21)
    await q.claim(10)
    assert await q.mark_failed("m1", "boom") == "failed"  # attempts=3 == max -> dead letter
    stats = await q.stats()
    assert stats["failed"] == 1 and stats["pending"] == 0

    assert await q.requeue_failed() == 1
    item = await q.get("m1")
    assert item and item.status == "pending" and item.attempts == 0

    # release() does not spend an attempt
    await q.claim(10)
    await q.release("m1", delay_seconds=5, kind="quota_exhausted")
    item = await q.get("m1")
    assert item and item.attempts == 0 and item.last_error_kind == "quota_exhausted"

    # crashed worker: an expired lease is re-claimable
    now[0] += timedelta(seconds=6)
    assert len(await q.claim(10)) == 1
    now[0] += timedelta(seconds=61)
    assert len(await q.claim(10)) == 1

    await q.mark_done("m1")
    assert await q.get("m1") is None


async def test_non_retryable_failure_dead_letters_immediately(stack) -> None:
    q = PendingEmbeddingQueue(stack["db"], max_attempts=10)
    await q.enqueue("m2", "u1")
    await q.claim()
    assert await q.mark_failed("m2", "input rejected", "bad_request", retryable=False) == "failed"


# ---------------------------------------------------------------------------
# Worker: drains after the provider recovers
# ---------------------------------------------------------------------------


async def test_worker_defers_while_provider_down_and_drains_after_recovery(stack) -> None:
    db, qdrant, embeddings, provider, clock = (
        stack["db"],
        stack["qdrant"],
        stack["embeddings"],
        stack["provider"],
        stack["clock"],
    )
    for i in range(3):
        await _save(db, f"00000000-0000-0000-0000-00000000000{i}", content=f"fact number {i} about Kingston")
    queue = PendingEmbeddingQueue(db)
    for i in range(3):
        await queue.enqueue(f"00000000-0000-0000-0000-00000000000{i}", "u1", reason="provider_down")
    worker = PendingEmbeddingWorker(queue, db, qdrant, embeddings, batch_size=10, provider_down_delay_seconds=0)

    provider.mode = "quota"
    r1 = await worker.run_once()
    assert r1["claimed"] == 3 and r1["deferred"] == 3 and r1["done"] == 0
    assert provider.calls == 1  # first item hit quota; the rest were handed back untouched
    assert embeddings.breaker.state == CircuitState.OPEN
    stats = await queue.stats()
    assert stats["pending"] == 3 and stats["failed"] == 0
    items = [await queue.get(f"00000000-0000-0000-0000-00000000000{i}") for i in range(3)]
    assert all(item is not None and item.attempts == 0 for item in items)

    r2 = await worker.run_once()
    assert r2["skipped"] == 1 and r2["claimed"] == 0  # breaker open: don't even claim
    assert provider.calls == 1

    # Provider recovers; breaker reset window passes -> one probe -> closes.
    provider.mode = "ok"
    clock.now += 901
    r3 = await worker.run_once()
    assert r3["done"] == 3, r3
    assert embeddings.breaker.state == CircuitState.CLOSED
    assert (await queue.stats())["pending"] == 0

    # Vectors + full payload are in Qdrant; rows are keyword-searchable.
    point = await qdrant.get_by_id("00000000-0000-0000-0000-000000000001")
    assert point is not None
    assert point["content"] == "fact number 1 about Kingston"
    assert point["user_id"] == "u1" and point["metadata"] == {"k": "v"}
    fts = await db.search_fts("Kingston", user_id="u1", project_id="default", limit=10)
    assert len(fts) == 3


async def test_worker_drops_deleted_and_source_rows(stack) -> None:
    db, qdrant, embeddings, provider = stack["db"], stack["qdrant"], stack["embeddings"], stack["provider"]
    await _save(db, "00000000-0000-0000-0000-0000000000aa", memory_type="source")
    queue = PendingEmbeddingQueue(db)
    await queue.enqueue("00000000-0000-0000-0000-0000000000aa", "u1")
    await queue.enqueue("00000000-0000-0000-0000-0000000000bb", "u1")  # no such memory
    worker = PendingEmbeddingWorker(queue, db, qdrant, embeddings)
    r = await worker.run_once()
    assert r["dropped"] == 2
    assert provider.calls == 0
    assert (await queue.stats())["pending"] == 0


async def test_worker_retries_when_vector_store_fails(stack) -> None:
    db, embeddings = stack["db"], stack["embeddings"]
    await _save(db, "00000000-0000-0000-0000-0000000000cc")
    queue = PendingEmbeddingQueue(db)
    await queue.enqueue("00000000-0000-0000-0000-0000000000cc", "u1")
    broken_qdrant = MagicMock()
    broken_qdrant.upsert = AsyncMock(side_effect=ConnectionError("qdrant down"))
    worker = PendingEmbeddingWorker(queue, db, broken_qdrant, embeddings)
    r = await worker.run_once()
    assert r["retry"] == 1
    item = await queue.get("00000000-0000-0000-0000-0000000000cc")
    assert item and item.attempts == 1 and item.last_error_kind == "vector_store"


async def test_worker_loop_start_stop_via_registry(stack) -> None:
    db, qdrant, embeddings = stack["db"], stack["qdrant"], stack["embeddings"]
    await _save(db, "00000000-0000-0000-0000-0000000000dd")
    queue = PendingEmbeddingQueue(db)
    await queue.enqueue("00000000-0000-0000-0000-0000000000dd", "u1")
    registry = TaskRegistry()
    worker = PendingEmbeddingWorker(queue, db, qdrant, embeddings, poll_seconds=0.01)
    worker.start(registry)
    for _ in range(200):
        if await queue.get("00000000-0000-0000-0000-0000000000dd") is None:
            break
        await asyncio.sleep(0.01)
    assert await queue.get("00000000-0000-0000-0000-0000000000dd") is None
    assert "pending-embedding-worker" in registry.names()
    await worker.stop()
    await registry.shutdown(timeout=1)
    assert registry.running == 0


async def test_restore_from_archive_enqueues_reembedding(stack) -> None:
    db = stack["db"]
    await _save(db, "00000000-0000-0000-0000-0000000000ee")
    assert await db.archive_memory("00000000-0000-0000-0000-0000000000ee", reason="manual")
    assert await db.restore_memory("00000000-0000-0000-0000-0000000000ee")
    item = await PendingEmbeddingQueue(db).get("00000000-0000-0000-0000-0000000000ee")
    assert item is not None and item.reason == "restored"


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


async def test_readiness_ok_then_degraded_on_quota(stack) -> None:
    clock = FakeClock()
    checker = ReadinessChecker(
        settings=MagicMock(enable_reranking=False),
        db=stack["db"],
        qdrant=stack["qdrant"],
        embeddings=stack["embeddings"],
        pending_queue=PendingEmbeddingQueue(stack["db"]),
        probe_interval=300,
        clock=clock,
    )
    body = await checker.check()
    assert body["status"] == "ok", body
    assert body["components"]["embeddings"]["probe"]["dimensions"] == DIMS
    assert body["components"]["sqlite"]["schema_version"] >= 2

    # Quota exhausted on a real request opens the breaker; readiness goes degraded
    # passively, without spending another provider call.
    stack["provider"].mode = "quota"
    with pytest.raises(EmbeddingProviderError):
        await stack["embeddings"].embed("something new")
    calls = stack["provider"].calls
    clock.now += 1000  # probe would be due, but the breaker is open
    body = await checker.check()
    assert body["status"] == "degraded"
    assert body["degraded_components"] == ["embeddings"]
    assert body["components"]["embeddings"]["reason"] == "quota_exhausted"
    assert stack["provider"].calls == calls


async def test_readiness_probe_is_cached_and_single_flight(stack) -> None:
    clock = FakeClock()
    gate = asyncio.Event()
    probes = 0

    async def slow_probe(text: str = "") -> int:
        nonlocal probes
        probes += 1
        await gate.wait()
        return DIMS

    stack["embeddings"].probe = slow_probe
    checker = ReadinessChecker(
        settings=MagicMock(enable_reranking=False),
        db=stack["db"],
        qdrant=stack["qdrant"],
        embeddings=stack["embeddings"],
        probe_interval=300,
        clock=clock,
    )
    tasks = [asyncio.create_task(checker.check()) for _ in range(20)]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert probes == 1
    assert all(r["status"] == "ok" for r in results)
    clock.now += 299
    await checker.check()
    assert probes == 1  # cached
    clock.now += 2
    await checker.check()
    assert probes == 2  # at most one probe per interval


async def test_readiness_reports_missing_key_and_dimension_mismatch(tmp_path) -> None:
    settings = Settings(openai_api_key=None, embedding_dimensions=8, qdrant_collection="dim_test")
    qdrant = QdrantStore(settings)
    qdrant._client = AsyncQdrantClient(location=":memory:")
    await qdrant.create_collection("dim_test", 3)
    await qdrant.init_collection()
    assert qdrant.dimension_status == {"collection": "dim_test", "expected": 8, "actual": 3, "ok": False}
    checker = ReadinessChecker(
        settings=MagicMock(enable_reranking=True),
        db=None,
        qdrant=qdrant,
        embeddings=EmbeddingService(settings),
    )
    body = await checker.check()
    comps = body["components"]
    assert body["status"] == "degraded"
    assert comps["embeddings"]["reason"] == "missing_credentials"
    assert comps["qdrant"]["reason"] == "dimension_mismatch"
    assert comps["sqlite"]["status"] == "degraded"
    await qdrant.close()


async def test_qdrant_init_retries_with_backoff(monkeypatch) -> None:
    store = QdrantStore(Settings(embedding_dimensions=DIMS, qdrant_collection="retry_test"))
    attempts = 0
    real = AsyncQdrantClient(location=":memory:")

    async def flaky_get_client():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("qdrant not up yet")
        return real

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(store, "_get_client", flaky_get_client)
    monkeypatch.setattr("remembra.storage.qdrant.asyncio.sleep", fake_sleep)
    await store.init_collection_with_retry(attempts=5, base_delay=1.0)
    assert sleeps == [1.0, 2.0]  # two failures, exponential backoff, then success
    assert await real.collection_exists("retry_test")

    attempts = -100
    with pytest.raises(ConnectionError):
        await store.init_collection_with_retry(attempts=2, base_delay=0.0)


async def test_qdrant_health_uses_client_transport() -> None:
    from remembra.core.health import check_qdrant

    store = MagicMock()
    store.health_check = AsyncMock(return_value=False)
    assert await check_qdrant(store) == {"status": "degraded"}
    store.health_check.assert_awaited_once()


# ---------------------------------------------------------------------------
# Metrics + alerts
# ---------------------------------------------------------------------------


async def test_metrics_endpoint_requires_token(monkeypatch) -> None:
    import remembra.config
    import remembra.main as main

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        monkeypatch.setattr(remembra.config, "_settings", Settings(metrics_token=None))
        assert (await client.get("/metrics")).status_code == 404
        monkeypatch.setattr(remembra.config, "_settings", Settings(metrics_token="s3cret"))
        assert (await client.get("/metrics", headers={"Authorization": "Bearer nope"})).status_code == 401
        r = await client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    for name in (
        "remembra_embedding_errors_total",
        "remembra_store_failures_total",
        "remembra_recall_degraded_total",
        "remembra_circuit_breaker_state",
        "remembra_pending_embeddings",
    ):
        assert f"# TYPE {name}" in r.text


async def test_quota_open_sends_one_webhook_alert_per_cooldown() -> None:
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        received.append(json.loads(request.content))
        return httpx.Response(200)

    clock = FakeClock()
    notifier = AlertNotifier(
        webhook_url="https://hooks.example.test/x",
        cooldown_seconds=3600,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )
    registry = TaskRegistry()
    listener = notifier.breaker_listener(lambda coro, name: registry.spawn(coro, name=name))
    register_state_listener(listener)
    try:
        b = CircuitBreaker("embeddings-alert-test", quota_reset_timeout=10, clock=clock)
        quota = EmbeddingProviderError("x", kind=ProviderErrorKind.QUOTA_EXHAUSTED)
        b.record_failure(quota)  # open -> alert
        clock.now += 11
        assert b.acquire() is True
        b.record_failure(quota, is_probe=True)  # re-open within cooldown -> suppressed
        b.record_failure(EmbeddingProviderError("x", kind=ProviderErrorKind.UNAVAILABLE))  # not alerting kind
        await asyncio.sleep(0.05)
        await registry.shutdown(timeout=1)
    finally:
        unregister_state_listener(listener)
    assert len(received) == 1
    assert received[0]["event"] == "embeddings-alert-test_quota_exhausted"
    assert "top it up" in received[0]["message"]


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------


async def test_task_registry_tracks_bounds_and_shuts_down() -> None:
    registry = TaskRegistry(max_concurrency=2)
    running = 0
    peak = 0
    release = asyncio.Event()

    async def job() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    async def forever() -> None:
        while True:
            await asyncio.sleep(1)

    async def explode() -> None:
        raise RuntimeError("background boom")

    for i in range(5):
        registry.spawn(job(), name=f"job-{i}", limited=True)
    registry.spawn(forever(), name="loop", loop_task=True)
    registry.spawn(explode(), name="explode")
    await asyncio.sleep(0.05)
    assert peak == 2  # concurrency bound
    assert "loop" in registry.names() and "explode" not in registry.names()
    release.set()
    await registry.shutdown(timeout=1)
    assert registry.running == 0
    with pytest.raises(RuntimeError):
        registry.spawn(job(), name="late")


# ---------------------------------------------------------------------------
# TTL cleanup loop archives instead of deleting
# ---------------------------------------------------------------------------


async def test_cleanup_archives_expired_across_projects(stack) -> None:
    from remembra.temporal.cleanup import TemporalCleanupJob

    db, qdrant = stack["db"], stack["qdrant"]
    past = utcnow() - timedelta(days=1)
    await _save(db, "00000000-0000-0000-0000-000000000e01", project_id="alpha", expires_at=past)
    await _save(db, "00000000-0000-0000-0000-000000000e02", project_id="beta", expires_at=past)
    await _save(db, "00000000-0000-0000-0000-000000000e03", project_id="beta")
    job = TemporalCleanupJob(database=db, qdrant_store=qdrant, archive_expired=True)
    result = await job.run_cleanup()
    assert result["expired_deleted"] == 2, result
    archived = await db.get_archived_memory("00000000-0000-0000-0000-000000000e02")
    assert archived is not None and archived["archive_reason"] == "ttl_expired"
    assert await db.get_memory("00000000-0000-0000-0000-000000000e03") is not None


# ---------------------------------------------------------------------------
# REL-20 serve_spa path traversal
# ---------------------------------------------------------------------------


def test_safe_static_file_blocks_traversal(tmp_path) -> None:
    from remembra.main import _safe_static_file

    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<html/>")
    (root / "assets").mkdir()
    (root / "assets" / "app.js").write_text("js")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    (root / "link").symlink_to(secret)
    r = root.resolve()
    assert _safe_static_file(r, "assets/app.js") == (root / "assets" / "app.js").resolve()
    assert _safe_static_file(r, "../secret.txt") is None
    assert _safe_static_file(r, "assets/../../secret.txt") is None
    assert _safe_static_file(r, str(secret)) is None  # absolute path
    assert _safe_static_file(r, "link") is None  # symlink escaping the root
    assert _safe_static_file(r, "assets") is None  # directory
    assert _safe_static_file(r, "a\x00b") is None


async def test_serve_spa_never_serves_files_outside_static_root(tmp_path, monkeypatch) -> None:
    import remembra.config
    import remembra.main as main

    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("INDEX")
    (tmp_path / "secret.txt").write_text("SECRET")
    monkeypatch.setenv("REMEMBRA_STATIC_DIR", str(root))
    monkeypatch.setattr(remembra.config, "_settings", None)
    app = main.create_app()
    monkeypatch.setattr(remembra.config, "_settings", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for path in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
            r = await client.get(path)
            assert "SECRET" not in r.text, path
        assert (await client.get("/index.html")).text == "INDEX"
        assert (await client.get("/some/spa/route")).text == "INDEX"


# ---------------------------------------------------------------------------
# Real lifespan boot: worker + loops start, readiness answers, clean shutdown
# ---------------------------------------------------------------------------


async def test_lifespan_boots_background_work_and_shuts_down(tmp_path, monkeypatch) -> None:
    import remembra.config
    import remembra.main as main

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'life.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_EMBEDDING_DIMENSIONS", str(DIMS))
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "life_test")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_PENDING_EMBEDDINGS_POLL_SECONDS", "0.01")
    # The TTL cleanup loop is opt-in (default off until prod rows are audited).
    monkeypatch.setenv("REMEMBRA_TEMPORAL_CLEANUP_ENABLED", "true")
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self):  # noqa: ANN001
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    provider = Provider()
    app = main.create_app()
    async with app.router.lifespan_context(app):
        embedder = app.state.embeddings._get_embedder()
        embedder._client = httpx.AsyncClient(transport=provider.transport())
        names = app.state.tasks.names()
        assert "pending-embedding-worker" in names
        assert "temporal-cleanup-loop" in names

        # queue drains in the running app
        await app.state.db.save_memory_metadata(
            memory_id="00000000-0000-0000-0000-00000000f001",
            user_id="u1",
            project_id="default",
            content="queued while the provider was down",
            extracted_facts=[],
            metadata={},
            created_at=utcnow(),
        )
        await app.state.pending_embeddings.enqueue("00000000-0000-0000-0000-00000000f001", "u1")
        for _ in range(300):
            if await app.state.pending_embeddings.get("00000000-0000-0000-0000-00000000f001") is None:
                break
            await asyncio.sleep(0.01)
        assert await app.state.pending_embeddings.get("00000000-0000-0000-0000-00000000f001") is None
        assert await app.state.qdrant.get_by_id("00000000-0000-0000-0000-00000000f001") is not None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.get("/health/ready")
            assert r.status_code == 200
            body = r.json()
            assert body["components"]["embeddings"]["status"] == "ok", body
            assert body["components"]["qdrant"]["status"] == "ok"
            r = await client.get("/health")
            assert r.status_code == 200
        tasks = app.state.tasks
    assert tasks.running == 0
    assert "remembra_background_tasks" in REGISTRY.render()


def test_temporal_cleanup_is_opt_in_by_default(monkeypatch) -> None:
    from remembra.config import Settings

    monkeypatch.delenv("REMEMBRA_TEMPORAL_CLEANUP_ENABLED", raising=False)
    assert Settings().temporal_cleanup_enabled is False
