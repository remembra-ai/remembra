"""The Remembra API as the crew E2E, load and soak suites run it (WP-15, spec §13.2–13.4).

    python -m tests.crew.e2e.server --workdir DIR --port PORT [--webhook-forward URL]

One real server process: the production routers (``api_router``: memories, relay, inbox, crew…),
``/ws`` at the root, and crew mode installed exactly as ``main.py`` does it
(``main.install_crew``), so every crew startup hook runs in the app lifespan: ``crew.db`` and the
outbox worker, the event bus and its WebSocket fan-out, the reaper, the claim sweep, the report
invariant job, notifications and retention. Auth is **on** (JWT and API keys, real RBAC).

What differs from production, and why:

* the vector store is Qdrant's in-process local mode and the embedder is a deterministic hashed
  bag of words (no network, no model): memory ingestion and ``recall_memories`` still run the real
  :class:`MemoryService` and SQLite paths;
* webhook notifications go through the real :class:`~remembra.crew.notify.WebhookSender` (signing,
  SSRF resolution, pinned IP, no redirects). Hosts ending in ``.e2e.test`` resolve to a
  documentation IP and the bytes are forwarded over plain HTTP to ``--webhook-forward`` (the test's
  receiver), because a local receiver cannot hold a publicly trusted certificate. Every other host
  goes to the production resolver, which refuses private addresses;
* a few test-only routes under ``/__e2e/`` expose server-side timings (for the §13.4 latency
  budgets), run a reaper sweep or a retention pass on demand, and report ``database is locked``.

On start it creates the owner (``owner@example.com``), a JWT and an admin API key, and prints one
JSON line ``{"port", "key", "jwt", "user_id"}``. Test-only; it binds 127.0.0.1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import remembra.config as config_module
import remembra.main as main_module
from remembra.api.router import api_router
from remembra.api.v1 import websocket
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.core.limiter import limiter
from remembra.core.tasks import TaskRegistry
from remembra.crew import startup
from remembra.crew.notify import WebhookSender
from remembra.inbox.manager import InboxManager
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from remembra.webhooks.manager import ResolvedTarget, resolve_webhook_target
from tests.agent_api_harness import OneFactExtractor
from tests.security_harness import make_settings

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "Str0ng!Passw0rd"
TEST_WEBHOOK_SUFFIX = ".e2e.test"
TEST_WEBHOOK_IP = "93.184.216.34"  # documentation address; the bytes are forwarded to the local receiver
EMBED_DIM = 64


# ---------------------------------------------------------------------------
# Memory backends: real service, in-process vector store, deterministic embedder
# ---------------------------------------------------------------------------


class HashedEmbeddings:
    """Bag-of-words hashed into ``EMBED_DIM`` buckets and L2-normalised: similar words, similar vectors."""

    dimensions = EMBED_DIM

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * EMBED_DIM
        for word in re.findall(r"[a-z0-9][a-z0-9_-]*", text.lower()):
            vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


async def memory_backend(settings: Any, db: Database) -> MemoryService:
    from qdrant_client import AsyncQdrantClient

    from remembra.storage.qdrant import QdrantStore

    store = QdrantStore(settings)
    store._client = AsyncQdrantClient(location=":memory:")
    await store.init_collection(EMBED_DIM)
    service = MemoryService(settings=settings, qdrant=store, db=db, embeddings=HashedEmbeddings())  # type: ignore[arg-type]
    service.extractor = OneFactExtractor()  # type: ignore[assignment]
    return service


# ---------------------------------------------------------------------------
# Webhooks: the real sender, a test-only resolver and a forwarding transport
# ---------------------------------------------------------------------------


async def e2e_resolver(url: str) -> ResolvedTarget:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host.endswith(TEST_WEBHOOK_SUFFIX):
        return ResolvedTarget(url=url, scheme="https", hostname=host, port=parts.port or 443, ips=(TEST_WEBHOOK_IP,))
    return await resolve_webhook_target(url)


class ForwardTransport(httpx.AsyncBaseTransport):
    """Sends the pinned request's exact bytes and headers to the local receiver over plain HTTP."""

    def __init__(self, target: str) -> None:
        self.target = httpx.URL(target)
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != TEST_WEBHOOK_IP:
            raise httpx.ConnectError(f"e2e server: refusing to forward to {request.url.host}")
        body = await request.aread()
        url = request.url.copy_with(scheme=self.target.scheme, host=self.target.host, port=self.target.port)
        forwarded = httpx.Request(request.method, url, headers=request.headers, content=body)
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self.inner.aclose()


# ---------------------------------------------------------------------------
# Server-side measurements (§13.4 budgets are server-side)
# ---------------------------------------------------------------------------

_TAG: contextvars.ContextVar[str | None] = contextvars.ContextVar("e2e_tx_tag", default=None)
_ID_RE = re.compile(r"/(?:cs|crw|clm|tsk|zn|msg|rpt|inb|col|hst|ntt|dec|zc)_[A-Za-z0-9_-]+")


def route_key(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or _ID_RE.sub("/{id}", str(scope.get("path") or ""))
    return f"{scope.get('method', 'WS')} {path}"


class LockedLog(logging.Handler):
    """Counts every log record that mentions ``database is locked`` (any logger)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.count = 0
        self.samples: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:
            text = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            text += f" {record.exc_info[1]}"
        if "database is locked" in text:
            self.count += 1
            if len(self.samples) < 5:
                self.samples.append(text[:300])


class Metrics:
    def __init__(self) -> None:
        self.reset()
        self.locked = LockedLog()

    def reset(self) -> None:
        self.durations: dict[str, list[float]] = defaultdict(list)
        self.statuses: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.sweeps: list[float] = []
        self.tx_holds: dict[str, list[float]] = defaultdict(list)
        self.errors: list[str] = []

    def summary(self) -> dict[str, Any]:
        def pct(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]

        routes = {
            key: {
                "count": len(v),
                "p50_ms": round(pct(v, 0.50) * 1000, 2),
                "p95_ms": round(pct(v, 0.95) * 1000, 2),
                "p99_ms": round(pct(v, 0.99) * 1000, 2),
                "max_ms": round(max(v) * 1000, 2),
                "statuses": dict(self.statuses[key]),
            }
            for key, v in sorted(self.durations.items())
        }
        return {
            "routes": routes,
            "sweeps_ms": [round(s * 1000, 2) for s in self.sweeps],
            "tx_holds": {tag: {"count": len(v), "max_ms": round(max(v) * 1000, 2)} for tag, v in self.tx_holds.items()},
            "database_locked": self.locked.count,
            "database_locked_samples": self.locked.samples,
            "errors": self.errors[:20],
        }


METRICS = Metrics()


class TimingMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or str(scope.get("path", "")).startswith("/__e2e/"):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 0}

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception as e:
            status["code"] = status["code"] or 500  # an unhandled error is a 500 to the client
            METRICS.errors.append(f"{route_key(scope)}: {type(e).__name__}: {e}"[:300])
            if "database is locked" in str(e):
                METRICS.locked.count += 1
            raise
        finally:
            key = route_key(scope)
            METRICS.durations[key].append(time.perf_counter() - started)
            METRICS.statuses[key][status["code"]] += 1


def instrument_transactions(crew_db: Any) -> None:
    """Time every ``crew.db`` transaction; tagged ones (retention) are reported separately."""
    original: Callable[[], Any] = crew_db.transaction

    @asynccontextmanager
    async def timed() -> AsyncIterator[None]:
        async with original():
            started = time.perf_counter()
            try:
                yield
            finally:
                METRICS.tx_holds[_TAG.get() or "all"].append(time.perf_counter() - started)

    crew_db.transaction = timed


def instrument_reaper(app: FastAPI) -> None:
    reaper = getattr(app.state, "crew_reaper", None)
    if reaper is None or getattr(reaper, "_e2e_timed", False):
        return
    original: Callable[..., Awaitable[Any]] = reaper.sweep

    async def timed(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            METRICS.sweeps.append(time.perf_counter() - started)

    reaper.sweep = timed
    reaper._e2e_timed = True


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def build_app(workdir: Path, *, webhook_forward: str | None = None) -> FastAPI:
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["REMEMBRA_CREW_DB_PATH"] = str(workdir / "crew" / "crew.db")
    os.environ.pop(startup.TAILER_ENV, None)
    settings = make_settings(auth_enabled=True, embedding_dimensions=EMBED_DIM, enable_entity_resolution=False)
    config_module._settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.tasks = TaskRegistry()
        db = Database(str(workdir / "remembra.db"))
        await db.connect()
        await db.init_schema()
        roles = RoleManager(db)
        await roles.init_schema()
        await UsageMeter(db).init_schema()
        inbox = InboxManager(db)
        app.state.db = db
        app.state.api_key_manager = APIKeyManager(db)
        app.state.role_manager = roles
        app.state.audit_logger = AuditLogger(db)
        app.state.memory_service = await memory_backend(settings, db)
        app.state.sanitizer = ContentSanitizer()
        app.state.pii_detector = None
        app.state.users = UserManager(db, settings.jwt_secret)
        app.state.inbox_manager = inbox
        if webhook_forward:
            app.state.crew_webhook_sender = WebhookSender(resolver=e2e_resolver, transport=ForwardTransport(webhook_forward))
        yield
        await app.state.tasks.shutdown(timeout=2.0)
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(TimingMiddleware)
    app.include_router(api_router)
    main_module.install_crew(app)
    app.include_router(websocket.router)  # /ws at the root, as main.create_app mounts it

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/__e2e/metrics")
    async def metrics() -> dict[str, Any]:
        instrument_reaper(app)
        return METRICS.summary()

    @app.post("/__e2e/metrics/reset")
    async def metrics_reset() -> dict[str, bool]:
        instrument_reaper(app)
        METRICS.reset()
        return {"ok": True}

    @app.post("/__e2e/sweep")
    async def sweep() -> dict[str, Any]:
        """One reaper sweep now (the reaper's own loop keeps running every 30 s)."""
        instrument_reaper(app)
        started = time.perf_counter()
        report = await app.state.crew_reaper.sweep()
        return {"report": report.as_dict(), "ms": round((time.perf_counter() - started) * 1000, 2)}

    @app.post("/__e2e/retention")
    async def retention(request: Request) -> dict[str, Any]:
        """A retention pass as if ``?days=N`` had passed; every transaction it opens is timed."""
        from remembra.crew.events import utc_now
        from remembra.crew.retention import policy_for_tier, run_retention

        days = float(request.query_params.get("days") or 400)
        policy_ = policy_for_tier(request.query_params.get("tier") or "free")  # a plan that prunes raw events
        token = _TAG.set("retention")
        started = time.perf_counter()
        try:

            async def policy(_owner: str) -> Any:
                return policy_

            report = await run_retention(app.state.crew_db, resolve_policy=policy, now=utc_now() + timedelta(days=days))
        finally:
            _TAG.reset(token)
        holds = METRICS.tx_holds.get("retention", [])
        return {
            "events_pruned": report.events_pruned,
            "errors": report.errors,
            "chain_errors": report.chain_errors,
            "ms": round((time.perf_counter() - started) * 1000, 2),
            "transactions": len(holds),
            "max_tx_ms": round(max(holds) * 1000, 2) if holds else 0.0,
        }

    return app


async def seed_owner(app: FastAPI) -> dict[str, str]:
    users: UserManager = app.state.users
    user, error = await users.create_user(email=OWNER_EMAIL, password=OWNER_PASSWORD)
    assert user is not None, error
    await app.state.db.update_user_email_verified(user.id, True)
    created = await app.state.api_key_manager.create_key(user_id=user.id, name="e2e-admin")
    await app.state.role_manager.assign_role(created.id, Role.ADMIN)
    return {"key": created.key, "jwt": users.create_jwt_token(user.id, OWNER_EMAIL), "user_id": user.id}


async def seed_users(app: FastAPI, count: int) -> list[str]:
    """``count`` more owners, one admin API key each (load: every crew belongs to its own user)."""
    keys: list[str] = []
    users: UserManager = app.state.users
    for i in range(count):
        user, error = await users.create_user(email=f"load{i}@example.com", password=OWNER_PASSWORD)
        assert user is not None, error
        created = await app.state.api_key_manager.create_key(user_id=user.id, name=f"load-{i}")
        await app.state.role_manager.assign_role(created.id, Role.ADMIN)
        keys.append(created.key)
    return keys


async def serve(workdir: Path, port: int, webhook_forward: str | None, users: int = 0) -> None:
    app = build_app(workdir, webhook_forward=webhook_forward)
    logging.getLogger().addHandler(METRICS.locked)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", ws_ping_interval=None)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
            raise RuntimeError("the e2e server stopped during start-up")
        await asyncio.sleep(0.05)
    instrument_transactions(app.state.crew_db)
    instrument_reaper(app)
    creds = await seed_owner(app)
    extra = await seed_users(app, users) if users else []
    print(json.dumps({"port": port, **creds, "keys": extra}), flush=True)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workdir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--webhook-forward")
    p.add_argument("--seed-users", type=int, default=0, help="extra owners with one admin key each (load tests)")
    args = p.parse_args(argv)
    asyncio.run(serve(Path(args.workdir), args.port, args.webhook_forward, args.seed_users))
    return 0


if __name__ == "__main__":
    sys.exit(main())
