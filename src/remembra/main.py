"""FastAPI application factory and entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import RequestResponseEndpoint

from remembra import __version__
from remembra.api.router import api_router
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import RoleManager
from remembra.cloud.metering import UsageMeter
from remembra.config import get_settings
from remembra.core.health import build_health_response, check_qdrant
from remembra.core.logging import configure_logging
from remembra.extraction.conflicts import ConflictManager, ConflictStrategy
from remembra.inbox.manager import InboxManager
from remembra.plugins.manager import PluginManager
from remembra.security.anomaly_detector import AnomalyDetector
from remembra.security.audit import AuditLogger
from remembra.security.error_sanitizer import sanitize_error_message
from remembra.security.pii_detector import PIIDetector
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingService
from remembra.storage.qdrant import QdrantStore
from remembra.storage.reindex import ReindexManager
from remembra.teams.manager import TeamManager
from remembra.webhooks.delivery import WebhookDelivery
from remembra.webhooks.manager import WebhookManager

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


# Import limiter from core module to avoid circular imports
from remembra.core.limiter import limiter  # noqa: E402

# ---------------------------------------------------------------------------
# Application State
# ---------------------------------------------------------------------------


class AppState:
    """Shared application state - storage and services."""

    qdrant: QdrantStore
    db: Database
    embeddings: EmbeddingService
    memory_service: MemoryService
    api_key_manager: APIKeyManager
    audit_logger: AuditLogger
    sanitizer: ContentSanitizer
    pii_detector: PIIDetector | None
    anomaly_detector: AnomalyDetector | None
    usage_meter: UsageMeter | None
    webhook_manager: WebhookManager | None
    conflict_manager: ConflictManager | None
    role_manager: RoleManager | None
    space_manager: SpaceManager | None
    team_manager: TeamManager | None
    inbox_manager: InboxManager | None
    reindex_manager: ReindexManager | None
    plugin_manager: PluginManager | None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Initialize OpenTelemetry tracing (soft dependency)
    from remembra.core.tracing import setup_tracing

    setup_tracing(settings)

    # CRITICAL: Force unique JWT secret in production (OWASP compliance)
    if not settings.debug:
        default_secrets = [
            "remembra-jwt-secret-change-in-production",
            "quickstart-dev-only-not-for-production",
            "changeme",
            "secret",
            "your-secret-key",
        ]
        if settings.jwt_secret in default_secrets:
            raise RuntimeError(
                "CRITICAL SECURITY ERROR: You must set REMEMBRA_JWT_SECRET "
                "to a unique value in production.\n"
                "Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        if len(settings.jwt_secret) < 32:
            raise RuntimeError("CRITICAL SECURITY ERROR: REMEMBRA_JWT_SECRET must be at least 32 characters.")

    log.info(
        "remembra_starting",
        version=__version__,
        port=settings.port,
        auth_enabled=settings.auth_enabled,
        rate_limit_enabled=settings.rate_limit_enabled,
    )

    # Initialize storage layer
    log.info("initializing_storage_layer")

    # Qdrant vector store
    app.state.qdrant = QdrantStore(settings)
    await app.state.qdrant.init_collection()

    # SQLite metadata database
    app.state.db = Database(settings.database_url)
    await app.state.db.connect()
    await app.state.db.init_schema()

    # Embedding service
    app.state.embeddings = EmbeddingService(settings)

    # Security services (Week 7)
    app.state.api_key_manager = APIKeyManager(app.state.db)
    app.state.audit_logger = AuditLogger(app.state.db)
    app.state.sanitizer = ContentSanitizer(
        trust_threshold=settings.trust_score_threshold,
        log_suspicious=True,
    )

    # PII Detection (OWASP ASI06 - Memory Poisoning)
    if settings.pii_detection_enabled:
        app.state.pii_detector = PIIDetector(
            enabled=True,
            mode=settings.pii_mode,
            exclusions=settings.pii_exclusions,
        )
        log.info("pii_detection_enabled", mode=settings.pii_mode)
    else:
        app.state.pii_detector = None

    # Anomaly Detection (OWASP ASI06 - Memory Poisoning)
    if settings.anomaly_detection_enabled:
        app.state.anomaly_detector = AnomalyDetector(
            db=app.state.db,
            enabled=True,
            rate_threshold=settings.anomaly_rate_threshold,
        )
        log.info("anomaly_detection_enabled", rate_threshold=settings.anomaly_rate_threshold)
    else:
        app.state.anomaly_detector = None

    # RBAC role manager
    app.state.role_manager = RoleManager(app.state.db)
    await app.state.role_manager.init_schema()
    log.info("rbac_enabled")

    # Memory service (business logic)
    app.state.memory_service = MemoryService(
        settings=settings,
        qdrant=app.state.qdrant,
        db=app.state.db,
        embeddings=app.state.embeddings,
    )

    # Cloud services (billing, metering, limits)
    if settings.cloud_enabled:
        app.state.usage_meter = UsageMeter(app.state.db)
        await app.state.usage_meter.init_schema()
        log.info(
            "cloud_enabled",
            stripe_configured=bool(settings.stripe_secret_key),
        )
    else:
        app.state.usage_meter = None

    # Conflict resolution
    if settings.conflict_detection_enabled:
        try:
            strategy = ConflictStrategy(settings.conflict_strategy)
        except ValueError:
            strategy = ConflictStrategy.UPDATE
        app.state.conflict_manager = ConflictManager(
            db=app.state.db,
            default_strategy=strategy,
        )
        await app.state.conflict_manager.init_schema()
        log.info("conflict_detection_enabled", strategy=strategy.value)
        # Inject into memory service so conflicts are tracked during store
        app.state.memory_service.conflict_manager = app.state.conflict_manager
    else:
        app.state.conflict_manager = None

    # Webhook event system
    if settings.webhooks_enabled:
        delivery = WebhookDelivery(
            timeout=settings.webhook_timeout,
            max_retries=settings.webhook_max_retries,
        )
        app.state.webhook_manager = WebhookManager(
            db=app.state.db,
            delivery=delivery,
        )
        await app.state.webhook_manager.init_schema()
        log.info("webhooks_enabled")
    else:
        app.state.webhook_manager = None

    # Memory Spaces (cross-agent sharing)
    app.state.space_manager = SpaceManager(app.state.db)
    await app.state.space_manager.init_schema()
    app.state.memory_service.space_manager = app.state.space_manager
    log.info("memory_spaces_enabled")

    # Team collaboration
    app.state.team_manager = TeamManager(app.state.db)
    await app.state.team_manager.init_schema()
    log.info("team_collaboration_enabled")

    # Agent inbox (targeted agent-to-agent delivery — issue #9)
    app.state.inbox_manager = InboxManager(app.state.db)
    await app.state.inbox_manager.init_schema()
    log.info("agent_inbox_enabled")

    # Re-indexing manager (embedding model migration)
    app.state.reindex_manager = ReindexManager(
        db=app.state.db,
        qdrant=app.state.qdrant,
        embeddings=app.state.embeddings,
    )
    await app.state.reindex_manager.init_schema()

    # Plugin system
    app.state.plugin_manager = PluginManager()
    # Register built-in plugin classes in the marketplace
    from remembra.plugins.builtin.auto_tagger import AutoTaggerPlugin
    from remembra.plugins.builtin.recall_logger import RecallLoggerPlugin
    from remembra.plugins.builtin.slack_notifier import SlackNotifierPlugin

    app.state.plugin_manager.register_class(SlackNotifierPlugin)
    app.state.plugin_manager.register_class(AutoTaggerPlugin)
    app.state.plugin_manager.register_class(RecallLoggerPlugin)
    log.info("plugin_system_enabled", registered=3)

    # Conversation Ingestion Service (Phase 1 - Critical Feature)
    from remembra.services.conversation_ingest import ConversationIngestService

    app.state.conversation_ingest = ConversationIngestService(
        settings=settings,
        memory_service=app.state.memory_service,
    )
    log.info("conversation_ingest_service_enabled")

    # Sleep-Time Compute Worker (Phase 3 - Major Differentiator)
    if settings.sleep_time_enabled:
        from remembra.services.sleep_time import SleepTimeWorker

        app.state.sleep_worker = SleepTimeWorker(
            settings=settings,
            memory_service=app.state.memory_service,
        )
        log.info(
            "sleep_time_worker_enabled",
            trigger=settings.sleep_time_trigger,
            interval_hours=settings.sleep_time_interval_hours,
        )

        # Schedule automatic runs if interval mode
        if settings.sleep_time_trigger == "interval":

            async def scheduled_consolidation() -> None:
                """Background task for scheduled consolidation."""
                import asyncio

                while True:
                    await asyncio.sleep(settings.sleep_time_interval_hours * 3600)
                    try:
                        report = await app.state.sleep_worker.run_consolidation()
                        log.info(
                            "scheduled_consolidation_completed",
                            memories_scanned=report.memories_scanned,
                            duplicates_merged=report.duplicates_merged,
                        )
                    except Exception as e:
                        log.error("scheduled_consolidation_failed", error=str(e))

            # Start background task
            import asyncio

            asyncio.create_task(scheduled_consolidation())
            log.info("sleep_time_scheduler_started")
    else:
        app.state.sleep_worker = None

    # p99 Calibration Cache (v0.13 - @aipracticalist feedback)
    # Load cached calibration or run new calibration on startup
    from remembra.core.calibration import (
        CalibrationCache,
        CalibrationConfig,
    )

    calibration_cache = CalibrationCache()
    calibration_config = CalibrationConfig(
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dimensions,
        qdrant_collection=settings.qdrant_collection,
        enable_hybrid=settings.enable_hybrid_search,
        enable_reranking=settings.enable_reranking,
    )

    if calibration_cache.is_valid(calibration_config):
        cached_calibration = calibration_cache.load()
        if cached_calibration:
            app.state.calibration = cached_calibration
            log.info(
                "calibration_cache_loaded",
                recall_p99_ms=cached_calibration.recall_p99_ms,
            )
    else:
        # Run calibration in background (don't block startup)
        log.info("calibration_cache_miss_will_run_async")
        app.state.calibration = None
        # Note: Actual calibration runs on first few requests
        # and gets saved to cache after warmup

    log.info("storage_layer_ready")

    yield

    # Cleanup
    log.info("remembra_shutdown")
    if app.state.plugin_manager:
        await app.state.plugin_manager.shutdown()
    # Close persistent HTTP clients
    if hasattr(app.state, "embeddings"):
        await app.state.embeddings.close()
    if hasattr(app.state, "webhook_manager") and app.state.webhook_manager and hasattr(app.state.webhook_manager, "_delivery"):
        await app.state.webhook_manager._delivery.close()
    await app.state.db.close()
    await app.state.qdrant.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    settings = get_settings()

    # Configure rate limiter storage
    if settings.rate_limit_enabled:
        storage_uri = settings.rate_limit_storage
        if storage_uri == "memory":
            storage_uri = "memory://"
        limiter._storage_uri = storage_uri
        log.info("rate_limiting_configured", storage=settings.rate_limit_storage)

    app = FastAPI(
        title="Remembra",
        description="Universal memory layer for AI applications.",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Add rate limiter to app state
    app.state.limiter = limiter

    # Add rate limit exception handler
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Validation error handler - sanitize validation messages
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """
        Handle validation errors with sanitized messages.
        Prevents leaking internal details in validation error responses.
        """
        # Build sanitized error details
        errors = []
        for error in exc.errors():
            # Keep field location and type, sanitize the message
            sanitized_error = {
                "loc": error.get("loc"),
                "type": error.get("type"),
                "msg": sanitize_error_message(error.get("msg", "Validation error")),
            }
            errors.append(sanitized_error)

        log.warning(
            "validation_error",
            path=request.url.path,
            errors=len(errors),
        )

        return JSONResponse(
            status_code=422,
            content={"detail": errors},
        )

    # Generic exception handler to sanitize error messages
    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        Catch unhandled exceptions and return a sanitized error.
        Don't leak internal details like stack traces or file paths.
        """
        # Log the full error for debugging
        log.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )

        # Return a generic error to the client
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An internal error occurred. Please try again or contact support.",
                "error_id": request.headers.get("x-request-id", "unknown"),
            },
        )

    # Add SlowAPI middleware for rate limiting
    app.add_middleware(SlowAPIMiddleware)

    # Rate limit headers middleware - adds X-RateLimit-* headers to responses
    # This is separate from SlowAPIMiddleware because slowapi's headers_enabled
    # requires Response parameters on endpoints which our API doesn't use.
    from starlette.middleware.base import BaseHTTPMiddleware as BaseMiddleware

    class RateLimitHeadersMiddleware(BaseMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            response = await call_next(request)

            # Add rate limit headers if rate limiting info is available
            if hasattr(request.state, "view_rate_limit") and request.state.view_rate_limit:
                try:
                    rate_limit_info = request.state.view_rate_limit
                    rate_limit_item = rate_limit_info[0]  # RateLimitItem
                    rate_limit_keys = rate_limit_info[1]  # List of keys

                    # Get window stats from the limiter
                    window_stats = limiter.limiter.get_window_stats(rate_limit_item, *rate_limit_keys)
                    reset_time = 1 + window_stats[0]
                    remaining = window_stats[1]

                    response.headers["X-RateLimit-Limit"] = str(rate_limit_item.amount)
                    response.headers["X-RateLimit-Remaining"] = str(remaining)
                    response.headers["X-RateLimit-Reset"] = str(reset_time)
                except Exception as e:
                    # Don't fail the request if header injection fails
                    log.debug("rate_limit_header_injection_failed", error=str(e))

            return response

    app.add_middleware(RateLimitHeadersMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Security headers middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            # Content Security Policy - API-focused policy
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            if not settings.debug:
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    # Instrument app for OpenTelemetry tracing (if enabled)
    from remembra.core.tracing import instrument_app

    instrument_app(app)

    # -----------------------------------------------------------------------
    # Health endpoints (outside versioned prefix for easy probe config)
    # -----------------------------------------------------------------------

    @app.get("/health", tags=["ops"], include_in_schema=False)
    # No rate limit on health - load balancers and orchestrators need unrestricted access
    async def health(request: Request) -> JSONResponse:
        cfg = get_settings()
        qdrant_status = await check_qdrant(cfg.qdrant_url)
        body: dict[str, Any] = build_health_response(
            version=__version__,
            qdrant=qdrant_status,
            encryption_enabled=bool(cfg.encryption_key),
            build_sha=cfg.build_sha,
        )
        status_code = 200 if body["status"] == "ok" else 503
        return JSONResponse(content=body, status_code=status_code)

    @app.get("/", tags=["ops"], include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "remembra", "version": __version__, "docs": "/docs"}

    # -----------------------------------------------------------------------
    # Well-Known endpoints (MCP server card for Smithery)
    # -----------------------------------------------------------------------
    @app.get("/.well-known/mcp/server-card.json", tags=["ops"], include_in_schema=False)
    async def mcp_server_card() -> JSONResponse:
        """Return MCP server card for Smithery and other registries."""
        import json
        from pathlib import Path

        # Try to load from file first
        card_path = Path(__file__).parent / "api" / "well_known" / "server-card.json"
        if card_path.exists():
            with open(card_path) as f:
                return JSONResponse(content=json.load(f))

        # Fallback inline card
        return JSONResponse(
            content={
                "serverInfo": {"name": "Remembra", "version": __version__},
                "authentication": {"required": True, "schemes": ["apiKey"]},
                "tools": [
                    {"name": "store_memory", "description": "Store in memory"},
                    {"name": "recall_memories", "description": "Search memories"},
                    {"name": "forget_memories", "description": "Delete memories"},
                    {"name": "health_check", "description": "Check server health"},
                    {"name": "ingest_conversation", "description": "Ingest conversation"},
                ],
                "resources": [{"uri": "memory://recent", "name": "Recent Memories"}],
                "prompts": [],
            }
        )

    # -----------------------------------------------------------------------
    # Versioned API routes
    # -----------------------------------------------------------------------
    app.include_router(api_router)

    # -----------------------------------------------------------------------
    # WebSocket routes (at root level for easy access)
    # -----------------------------------------------------------------------
    from remembra.api.v1.websocket import router as ws_router

    app.include_router(ws_router)

    # -----------------------------------------------------------------------
    # Static files (Dashboard UI)
    # -----------------------------------------------------------------------
    static_dir = settings.static_dir
    if static_dir:
        from pathlib import Path

        static_path = Path(static_dir)
        if static_path.exists() and static_path.is_dir():
            from fastapi.responses import FileResponse
            from fastapi.staticfiles import StaticFiles

            # Serve static files at /static
            app.mount("/static", StaticFiles(directory=static_path), name="static")

            # Serve index.html for SPA routes
            @app.get("/{full_path:path}", include_in_schema=False)
            async def serve_spa(full_path: str) -> Response:
                # Don't intercept API routes or WebSocket
                api_paths = ("api/", "docs", "redoc", "openapi", "ws", "health")
                if any(full_path.startswith(p) for p in api_paths):
                    return JSONResponse({"detail": "Not found"}, status_code=404)

                # Try to serve the file directly
                file_path = static_path / full_path
                if file_path.exists() and file_path.is_file():
                    return FileResponse(file_path)

                # Fall back to index.html for SPA routing
                index_path = static_path / "index.html"
                if index_path.exists():
                    return FileResponse(index_path)

                return JSONResponse({"detail": "Not found"}, status_code=404)

            log.info("static_files_enabled", path=str(static_path))

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Dependency helpers for routes
# ---------------------------------------------------------------------------


def get_memory_service() -> MemoryService:
    """Dependency to get the memory service from app state."""
    return app.state.memory_service


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "remembra.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
