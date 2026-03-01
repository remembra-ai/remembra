"""FastAPI application factory and entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from remembra import __version__
from remembra.api.router import api_router
from remembra.auth.keys import APIKeyManager
from remembra.config import get_settings
from remembra.core.health import build_health_response, check_qdrant
from remembra.core.logging import configure_logging
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingService
from remembra.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


# Import limiter from core module to avoid circular imports
from remembra.core.limiter import limiter


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


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)
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

    # Memory service (business logic)
    app.state.memory_service = MemoryService(
        settings=settings,
        qdrant=app.state.qdrant,
        db=app.state.db,
        embeddings=app.state.embeddings,
    )

    log.info("storage_layer_ready")

    yield

    # Cleanup
    log.info("remembra_shutdown")
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Health endpoints (outside versioned prefix for easy probe config)
    # -----------------------------------------------------------------------

    @app.get("/health", tags=["ops"], include_in_schema=False)
    @limiter.limit("120/minute")
    async def health(request: Request) -> JSONResponse:
        cfg = get_settings()
        qdrant_status = await check_qdrant(cfg.qdrant_url)
        body: dict[str, Any] = build_health_response(
            version=__version__,
            qdrant=qdrant_status,
        )
        status_code = 200 if body["status"] == "ok" else 503
        return JSONResponse(content=body, status_code=status_code)

    @app.get("/", tags=["ops"], include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "remembra", "version": __version__, "docs": "/docs"}

    # -----------------------------------------------------------------------
    # Versioned API routes
    # -----------------------------------------------------------------------
    app.include_router(api_router)

    # -----------------------------------------------------------------------
    # Static files (Dashboard UI)
    # -----------------------------------------------------------------------
    static_dir = settings.static_dir
    if static_dir:
        from pathlib import Path
        static_path = Path(static_dir)
        if static_path.exists() and static_path.is_dir():
            from fastapi.staticfiles import StaticFiles
            from fastapi.responses import FileResponse
            
            # Serve static files at /static
            app.mount("/static", StaticFiles(directory=static_path), name="static")
            
            # Serve index.html for SPA routes
            @app.get("/{full_path:path}", include_in_schema=False)
            async def serve_spa(full_path: str):
                # Don't intercept API routes
                if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("redoc") or full_path.startswith("openapi"):
                    return JSONResponse({"error": "Not found"}, status_code=404)
                
                # Try to serve the file directly
                file_path = static_path / full_path
                if file_path.exists() and file_path.is_file():
                    return FileResponse(file_path)
                
                # Fall back to index.html for SPA routing
                index_path = static_path / "index.html"
                if index_path.exists():
                    return FileResponse(index_path)
                
                return JSONResponse({"error": "Not found"}, status_code=404)
            
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
