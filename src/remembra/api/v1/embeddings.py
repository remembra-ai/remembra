"""Embedding management endpoints – /api/v1/embeddings.

Provides:
- List supported embedding providers
- Get current provider/model info
- Switch provider (hot-swap) and trigger re-indexing
- Re-index job status and management
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.auth.superadmin import RequireSuperadmin
from remembra.core.limiter import limiter
from remembra.storage.embeddings import MODEL_DIMENSIONS, EmbeddingService
from remembra.storage.reindex import ReindexManager

router = APIRouter(prefix="/embeddings", tags=["embeddings"])

log = structlog.get_logger(__name__)

# Everything a provider switch can mutate on the shared, process-wide service.
_SETTINGS_FIELDS = (
    "openai_api_key",
    "voyage_api_key",
    "jina_api_key",
    "cohere_api_key",
    "azure_openai_api_key",
    "azure_openai_endpoint",
    "azure_openai_deployment",
    "embedding_dimensions",
)


def _snapshot(service: EmbeddingService) -> dict[str, Any]:
    """Capture the complete embedding configuration so a failed switch can be undone exactly."""
    settings = service.settings
    return {
        "provider": service._current_provider,
        "model": service._current_model,
        "embedder": service._embedder,
        "settings": {name: getattr(settings, name, None) for name in _SETTINGS_FIELDS if hasattr(settings, name)},
    }


async def _restore(service: EmbeddingService, snapshot: dict[str, Any]) -> None:
    """Roll the service back to ``snapshot`` (provider, model, live client and every key)."""
    candidate = service._embedder
    service._current_provider = snapshot["provider"]
    service._current_model = snapshot["model"]
    for name, value in snapshot["settings"].items():
        setattr(service.settings, name, value)
    service._embedder = snapshot["embedder"]
    if candidate is not None and candidate is not snapshot["embedder"]:
        try:
            await candidate.close()
        except Exception as e:  # pragma: no cover - best-effort client cleanup
            log.warning("embedder_close_failed", error_type=type(e).__name__)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_embedding_service(request: Request) -> EmbeddingService:
    service: EmbeddingService = request.app.state.embeddings
    return service


def get_reindex_manager(request: Request) -> ReindexManager:
    manager: ReindexManager | None = getattr(request.app.state, "reindex_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Re-indexing service is not available. Check server configuration.",
        )
    return manager


EmbeddingServiceDep = Annotated[EmbeddingService, Depends(get_embedding_service)]
ReindexManagerDep = Annotated[ReindexManager, Depends(get_reindex_manager)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProviderInfo(BaseModel):
    provider: str
    model: str
    dimensions: int
    supported_providers: list[str]


class SwitchProviderRequest(BaseModel):
    provider: str = Field(..., description="New embedding provider (openai, voyage, jina, cohere, ollama, azure_openai)")
    model: str | None = Field(None, description="Model name (uses provider default if omitted)")
    api_key: str | None = Field(
        None,
        description="Not accepted: provider keys must come from server configuration (REMEMBRA_*_API_KEY).",
    )
    auto_reindex: bool = Field(True, description="Automatically start re-indexing all memories")
    force: bool = Field(False, description="Force switch even if dimensions differ (DANGEROUS - may brick account)")


class SwitchProviderResponse(BaseModel):
    old_provider: str
    old_model: str
    new_provider: str
    new_model: str
    reindex_job_id: str | None = None
    message: str


class ReindexJobResponse(BaseModel):
    id: str
    old_provider: str
    old_model: str
    new_provider: str
    new_model: str
    total_memories: int
    processed: int
    failed: int
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/info",
    response_model=ProviderInfo,
    summary="Get current embedding provider info",
)
@limiter.limit("30/minute")
async def get_embedding_info(
    request: Request,
    embedding_service: EmbeddingServiceDep,
    current_user: CurrentUser,
) -> ProviderInfo:
    """Return the current embedding provider, model, and supported providers."""
    info = embedding_service.get_info()
    return ProviderInfo(**info)


@router.get(
    "/providers",
    summary="List supported embedding providers",
)
@limiter.limit("30/minute")
async def list_providers(
    request: Request,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """List all supported embedding providers and their models."""
    return {
        "providers": {
            "openai": {
                "models": ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
                "requires": "REMEMBRA_OPENAI_API_KEY",
                "description": "OpenAI embeddings (default)",
            },
            "azure_openai": {
                "models": ["Configured via deployment name"],
                "requires": "REMEMBRA_AZURE_OPENAI_API_KEY + REMEMBRA_AZURE_OPENAI_ENDPOINT + REMEMBRA_AZURE_OPENAI_DEPLOYMENT",
                "description": "Azure-hosted OpenAI embeddings (enterprise)",
            },
            "voyage": {
                "models": ["voyage-3", "voyage-3-lite", "voyage-code-3"],
                "requires": "REMEMBRA_VOYAGE_API_KEY",
                "description": "Voyage AI — best-in-class for code embeddings",
            },
            "jina": {
                "models": ["jina-embeddings-v3", "jina-embeddings-v2-base-en", "jina-embeddings-v2-small-en"],
                "requires": "REMEMBRA_JINA_API_KEY",
                "description": "Jina AI — multilingual with 8192-token context",
            },
            "cohere": {
                "models": ["embed-english-v3.0", "embed-multilingual-v3.0", "embed-english-light-v3.0"],
                "requires": "REMEMBRA_COHERE_API_KEY",
                "description": "Cohere embeddings",
            },
            "ollama": {
                "models": ["nomic-embed-text", "mxbai-embed-large", "all-minilm"],
                "requires": "Local Ollama server (REMEMBRA_OLLAMA_URL)",
                "description": "Local/private embeddings via Ollama",
            },
        },
        "known_dimensions": MODEL_DIMENSIONS,
    }


@router.post(
    "/switch",
    response_model=SwitchProviderResponse,
    summary="Switch embedding provider",
)
@limiter.limit("3/minute")
async def switch_provider(
    request: Request,
    body: SwitchProviderRequest,
    embedding_service: EmbeddingServiceDep,
    reindex_manager: ReindexManagerDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> SwitchProviderResponse:
    """Hot-swap the embedding provider/model. **Platform superadmin only.**

    The embedding service is shared by every tenant, so this is a platform
    operation. Provider API keys are never taken from the request; configure
    them in the server environment. If the new provider fails its test
    embedding, the dimension check, or re-index start, the complete previous
    configuration (provider, model, live client and every key) is restored.
    """
    if body.api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provider API keys cannot be set via the API. Configure REMEMBRA_*_API_KEY on the server.",
        )

    old_provider = embedding_service.provider
    old_model = embedding_service.model
    old_dims = embedding_service.dimensions

    # Verify the new provider is valid
    valid = {"openai", "azure_openai", "ollama", "cohere", "voyage", "jina"}
    if body.provider.lower() not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider: {body.provider}. Valid: {', '.join(sorted(valid))}",
        )

    # Check dimensions compatibility before switching
    new_model_key = f"{body.provider}:{body.model}" if body.model else body.provider
    new_dims = (MODEL_DIMENSIONS.get(body.model) if body.model else None) or MODEL_DIMENSIONS.get(new_model_key)

    if new_dims and old_dims and new_dims != old_dims and not body.force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Dimension mismatch: current model uses {old_dims}D, new model uses {new_dims}D. "
            f"Switching will require reindexing all memories. If reindex fails, your account "
            f"will be unable to store new memories until fixed. Add 'force: true' to proceed.",
        )

    snapshot = _snapshot(embedding_service)
    embedding_service.switch_provider(provider=body.provider, model=body.model)

    # Verify the new provider works by doing a test embedding
    try:
        test_embedding = await embedding_service.embed("test")
        actual_dims = len(test_embedding)
    except Exception as e:
        await _restore(embedding_service, snapshot)
        log.warning("embedding_switch_test_failed", provider=body.provider, error_type=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to connect to {body.provider}. Previous provider restored.",
        ) from e

    # Double-check actual dimensions match expectations
    if old_dims and actual_dims != old_dims and not body.force:
        await _restore(embedding_service, snapshot)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Actual embedding dimensions ({actual_dims}D) differ from current ({old_dims}D). "
            f"This would break existing memories. Add 'force: true' to proceed anyway.",
        )

    reindex_job_id = None
    if body.auto_reindex:
        try:
            job = await reindex_manager.start_reindex(
                old_provider=old_provider,
                old_model=old_model,
                new_provider=embedding_service.provider,
                new_model=embedding_service.model,
            )
            reindex_job_id = job.id
        except RuntimeError as e:
            await _restore(embedding_service, snapshot)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Reindex job already running. Rolled back provider switch.",
            ) from e
        except Exception as e:
            await _restore(embedding_service, snapshot)
            log.error("reindex_start_failed", error_type=type(e).__name__, error=str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to start reindex job. Rolled back provider switch.",
            ) from e

    return SwitchProviderResponse(
        old_provider=old_provider,
        old_model=old_model,
        new_provider=embedding_service.provider,
        new_model=embedding_service.model or body.model or "",
        reindex_job_id=reindex_job_id,
        message="Provider switched" + (" — re-indexing started" if reindex_job_id else ""),
    )


# ---------------------------------------------------------------------------
# Re-index management
# ---------------------------------------------------------------------------


@router.get(
    "/reindex/status",
    response_model=ReindexJobResponse | None,
    summary="Get current reindex job status",
)
@limiter.limit("30/minute")
async def reindex_status(
    request: Request,
    reindex_manager: ReindexManagerDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    job_id: str | None = Query(None, description="Job ID (defaults to current job)"),
) -> ReindexJobResponse | dict[str, str]:
    """Get the status of a re-indexing job."""
    job = await reindex_manager.get_status(job_id)
    if job is None:
        return {"message": "No reindex job found"}
    return ReindexJobResponse(
        id=job.id,
        old_provider=job.old_provider,
        old_model=job.old_model,
        new_provider=job.new_provider,
        new_model=job.new_model,
        total_memories=job.total_memories,
        processed=job.processed,
        failed=job.failed,
        status=job.status,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )


@router.post(
    "/reindex/cancel",
    summary="Cancel a running reindex job",
)
@limiter.limit("5/minute")
async def cancel_reindex(
    request: Request,
    reindex_manager: ReindexManagerDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> dict[str, Any]:
    """Cancel the currently running re-indexing job."""
    cancelled = await reindex_manager.cancel()
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No running reindex job to cancel",
        )
    return {"cancelled": True}


@router.get(
    "/reindex/history",
    summary="List past reindex jobs",
)
@limiter.limit("15/minute")
async def reindex_history(
    request: Request,
    reindex_manager: ReindexManagerDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List recent re-indexing jobs."""
    jobs = await reindex_manager.list_jobs(limit=limit)
    return {"jobs": jobs, "count": len(jobs)}
