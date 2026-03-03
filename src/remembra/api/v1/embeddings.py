"""Embedding management endpoints – /api/v1/embeddings.

Provides:
- List supported embedding providers
- Get current provider/model info
- Switch provider (hot-swap) and trigger re-indexing
- Re-index job status and management
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.storage.embeddings import MODEL_DIMENSIONS, EmbeddingService
from remembra.storage.reindex import ReindexManager

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_embedding_service(request: Request) -> EmbeddingService:
    return request.app.state.embeddings


def get_reindex_manager(request: Request) -> ReindexManager:
    manager = getattr(request.app.state, "reindex_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Re-indexing is not available",
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
    api_key: str | None = Field(None, description="API key (uses env var if omitted)")
    auto_reindex: bool = Field(True, description="Automatically start re-indexing all memories")


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
) -> SwitchProviderResponse:
    """Hot-swap the embedding provider/model.

    When ``auto_reindex`` is true (default), a background job is started
    to re-embed all memories with the new model.  Without re-indexing,
    recall quality will degrade because old vectors are incompatible
    with the new model.
    """
    old_provider = embedding_service.provider
    old_model = embedding_service.model

    # Verify the new provider is valid
    valid = {"openai", "azure_openai", "ollama", "cohere", "voyage", "jina"}
    if body.provider.lower() not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider: {body.provider}. Valid: {', '.join(sorted(valid))}",
        )

    # Switch the provider
    embedding_service.switch_provider(
        provider=body.provider,
        model=body.model,
        api_key=body.api_key,
    )

    # Verify the new provider works by doing a test embedding
    try:
        await embedding_service.embed("test")
    except Exception as e:
        # Roll back
        embedding_service.switch_provider(provider=old_provider, model=old_model)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to connect to {body.provider}: {e}",
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
            # Job already running
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

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
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List recent re-indexing jobs."""
    jobs = await reindex_manager.list_jobs(limit=limit)
    return {"jobs": jobs, "count": len(jobs)}
