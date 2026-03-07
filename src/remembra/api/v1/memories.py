"""Memory CRUD endpoints – /api/v1/memories."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from remembra.auth.middleware import CurrentUser, get_client_ip
from remembra.cloud.limits import (
    EnforceRecallLimit,
    EnforceStoreLimit,
    record_delete_usage,
    record_recall_usage,
    record_store_usage,
)
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter
from remembra.models.memory import (
    BatchRecallRequest,
    BatchRecallResponse,
    BatchStoreRequest,
    BatchStoreResponse,
    BatchStoreResult,
    ForgetResponse,
    RecallRequest,
    RecallResponse,
    StoreRequest,
    StoreResponse,
    UpdateRequest,
    UpdateResponse,
)
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.webhooks.events import (
    WebhookEvent,
    memory_deleted_event,
    memory_recalled_event,
    memory_stored_event,
)

_webhook_log = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    return request.app.state.memory_service


def get_audit_logger(request: Request) -> AuditLogger:
    """Dependency to get the audit logger from app state."""
    return request.app.state.audit_logger


def get_sanitizer(request: Request) -> ContentSanitizer:
    """Dependency to get the content sanitizer from app state."""
    return request.app.state.sanitizer


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]
SanitizerDep = Annotated[ContentSanitizer, Depends(get_sanitizer)]


async def _dispatch_webhook(request: Request, event: WebhookEvent) -> None:
    """Fire-and-forget webhook dispatch. No-ops if webhooks are disabled."""
    manager = getattr(request.app.state, "webhook_manager", None)
    if manager is None:
        return
    try:
        await manager.dispatch(event)
    except Exception as exc:  # noqa: BLE001
        _webhook_log.warning("Webhook dispatch failed: %s", exc)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=StoreResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a new memory",
)
@limiter.limit("30/minute")
async def store_memory(
    request: Request,
    body: StoreRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    sanitizer: SanitizerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
    _limit: EnforceStoreLimit = None,
) -> StoreResponse:
    """
    Accept raw text, extract facts and entities, embed, and persist.
    
    - **content**: The text content to memorize
    - **project_id**: Optional project namespace (default: "default")
    - **metadata**: Optional key-value metadata
    - **ttl**: Optional time-to-live (e.g., "30d", "1y")
    
    Note: user_id is determined by API key (cannot be overridden).
    Rate limit: 30 requests/minute.
    """
    # Override user_id with authenticated user (security: prevent user spoofing)
    body.user_id = current_user.user_id
    
    # Sanitize content and compute trust score
    sanitization = None
    if settings.sanitization_enabled:
        sanitization = sanitizer.analyze(body.content, source="user_input")
    
    try:
        result = await memory_service.store(
            body,
            source="user_input",
            trust_score=sanitization.trust_score if sanitization else 1.0,
            checksum=sanitization.checksum if sanitization else None,
        )
        
        # Audit log (don't log content, only memory_id)
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id=result.id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=True,
        )

        # Record usage for metering (no-op if cloud disabled)
        await record_store_usage(request, current_user.user_id)

        # Dispatch webhook event (no-op if webhooks disabled)
        await _dispatch_webhook(
            request,
            memory_stored_event(
                user_id=current_user.user_id,
                memory_id=result.id,
                extracted_facts=getattr(result, "facts", None),
                entities=getattr(result, "entities", None),
                project_id=body.project_id or "default",
            ),
        )

        # Attach usage warning if set by cloud limit enforcement
        usage_warning = getattr(request.state, "usage_warning", None)
        if usage_warning is not None:
            result.usage_warning = usage_warning

        return result

    except ValueError as e:
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id="",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id="",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store memory: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Batch Store
# ---------------------------------------------------------------------------


@router.post(
    "/batch",
    response_model=BatchStoreResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store multiple memories in one request",
)
@limiter.limit("5/minute")
async def batch_store(
    request: Request,
    body: BatchStoreRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    sanitizer: SanitizerDep,
    current_user: CurrentUser,
) -> BatchStoreResponse:
    """
    Store up to 100 memories in a single request.
    
    Partial success is supported - failed items don't block successful ones.
    Each item is processed independently with its own result.
    
    **Request:**
    ```json
    {
      "items": [
        {"content": "Memory 1", "project_id": "default"},
        {"content": "Memory 2", "metadata": {"key": "value"}}
      ]
    }
    ```
    
    **Response includes:**
    - `results`: Per-item success/failure with responses or errors
    - `total`: Total items requested
    - `succeeded`: Count of successful stores
    - `failed`: Count of failed stores
    
    Rate limit: 5 requests/minute.
    """
    results: list[BatchStoreResult] = []
    succeeded = 0
    
    for i, item in enumerate(body.items):
        try:
            # Enforce authenticated user
            item.user_id = current_user.user_id
            
            resp = await memory_service.store(item)
            results.append(BatchStoreResult(index=i, success=True, response=resp))
            succeeded += 1
        except Exception as e:
            results.append(BatchStoreResult(index=i, success=False, error=str(e)))
    
    await audit_logger.log_memory_store(
        user_id=current_user.user_id,
        memory_id=f"batch:{succeeded}/{len(body.items)}",
        api_key_id=current_user.api_key_id,
        ip_address=get_client_ip(request),
        success=True,
    )
    
    return BatchStoreResponse(
        results=results,
        total=len(body.items),
        succeeded=succeeded,
        failed=len(body.items) - succeeded,
    )


# ---------------------------------------------------------------------------
# Batch Recall
# ---------------------------------------------------------------------------


@router.post(
    "/batch/recall",
    response_model=BatchRecallResponse,
    summary="Recall for multiple queries in one request",
)
@limiter.limit("10/minute")
async def batch_recall(
    request: Request,
    body: BatchRecallRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> BatchRecallResponse:
    """
    Execute up to 20 recall queries in a single request.
    
    Useful for:
    - Fetching context for multiple topics at once
    - Parallel memory lookups
    - Reducing API call overhead
    
    **Request:**
    ```json
    {
      "queries": [
        {"query": "What do we know about project X?"},
        {"query": "Recent conversations with client Y", "limit": 10}
      ]
    }
    ```
    
    Rate limit: 10 requests/minute.
    """
    results: list[RecallResponse] = []
    
    for query in body.queries:
        # Enforce authenticated user
        query.user_id = current_user.user_id
        
        resp = await memory_service.recall(query)
        results.append(resp)
    
    return BatchRecallResponse(
        results=results,
        total=len(body.queries),
    )


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


@router.post(
    "/recall",
    response_model=RecallResponse,
    summary="Retrieve memories relevant to a query",
)
@limiter.limit("60/minute")
async def recall_memories(
    request: Request,
    body: RecallRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    _limit: EnforceRecallLimit = None,
) -> RecallResponse:
    """
    Embed the query, perform semantic search, synthesise a context string.
    
    Uses advanced retrieval (v0.4.0):
    - Hybrid search (semantic + BM25 keyword matching)
    - Graph-aware retrieval (entity relationships)
    - Relevance ranking (recency, entity, keyword boosts)
    - Context window optimization (smart truncation)
    
    - **query**: Natural language query
    - **project_id**: Optional project namespace (default: "default")
    - **limit**: Maximum results to return (1-50, default: 5)
    - **threshold**: Minimum relevance score (0.0-1.0, default: 0.40)
    - **max_tokens**: Maximum tokens in context output (optional)
    
    Note: user_id is determined by API key (cannot be overridden).
    Rate limit: 60 requests/minute.
    """
    # Override user_id with authenticated user
    body.user_id = current_user.user_id
    
    try:
        result = await memory_service.recall(body)
        
        # Audit log
        await audit_logger.log_memory_recall(
            user_id=current_user.user_id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=True,
        )

        # Record usage for metering (no-op if cloud disabled)
        await record_recall_usage(request, current_user.user_id)

        # Dispatch webhook event (no-op if webhooks disabled)
        await _dispatch_webhook(
            request,
            memory_recalled_event(
                user_id=current_user.user_id,
                query=body.query,
                result_count=len(result.memories) if result.memories else 0,
                project_id=body.project_id or "default",
            ),
        )

        # Attach usage warning if set by cloud limit enforcement
        usage_warning = getattr(request.state, "usage_warning", None)
        if usage_warning is not None:
            result.usage_warning = usage_warning

        return result

    except Exception as e:
        await audit_logger.log_memory_recall(
            user_id=current_user.user_id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to recall memories: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Get by ID
# ---------------------------------------------------------------------------


@router.get(
    "/{memory_id}",
    summary="Get a specific memory by ID",
)
async def get_memory(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> dict:
    """
    Retrieve a specific memory by its ID.
    
    Note: Can only access memories belonging to the authenticated user.
    """
    result = await memory_service.get(memory_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )
    
    # Security: Verify memory belongs to authenticated user
    if result.get("user_id") != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",  # Don't reveal it exists
        )
    
    return result


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{memory_id}",
    response_model=UpdateResponse,
    summary="Update an existing memory",
)
@limiter.limit("20/minute")
async def update_memory(
    request: Request,
    memory_id: str,
    body: UpdateRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
) -> UpdateResponse:
    """
    Re-extract facts from updated content and merge entity graph.
    
    - **content**: New text content for the memory
    - **metadata**: Optional metadata to merge with existing
    
    The endpoint will:
    1. Re-extract facts from the new content
    2. Re-generate embeddings
    3. Update the vector store and database
    4. Re-extract and link entities
    
    Rate limit: 20 requests/minute.
    """
    try:
        result = await memory_service.update(
            memory_id=memory_id,
            user_id=current_user.user_id,
            new_content=body.content,
            new_metadata=body.metadata,
        )
        await audit_logger.log(
            "memory_updated",
            user_id=current_user.user_id,
            resource_id=memory_id,
            success=True,
        )
        return result
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )
    except Exception as e:
        await audit_logger.log(
            "memory_updated",
            user_id=current_user.user_id,
            resource_id=memory_id,
            success=False,
            error_message=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update memory",
        )


# ---------------------------------------------------------------------------
# Forget (delete)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cleanup Expired
# ---------------------------------------------------------------------------


@router.post(
    "/cleanup-expired",
    summary="Clean up expired memories",
)
@limiter.limit("5/minute")
async def cleanup_expired(
    request: Request,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
) -> dict:
    """
    Delete all expired memories (TTL-based cleanup).
    
    This endpoint should be called periodically to clean up
    memories that have exceeded their time-to-live.
    
    Rate limit: 5 requests/minute.
    """
    try:
        deleted = await memory_service.cleanup_expired(
            user_id=current_user.user_id,
        )
        
        await audit_logger.log_memory_forget(
            user_id=current_user.user_id,
            resource_id=f"expired:{deleted}",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=True,
        )
        
        return {"deleted_count": deleted}
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup expired memories: {str(e)}",
        )


@router.delete(
    "",
    response_model=ForgetResponse,
    summary="Forget memories (GDPR-compliant deletion)",
)
@limiter.limit("10/minute")
async def forget_memories(
    request: Request,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    memory_id: Annotated[
        str | None, Query(description="Delete a specific memory by ID")
    ] = None,
    entity: Annotated[
        str | None, Query(description="Delete all memories about an entity")
    ] = None,
    all_memories: Annotated[
        bool, Query(description="Delete all memories for the user")
    ] = False,
) -> ForgetResponse:
    """
    Delete memories matching the given filter.

    At least one of `memory_id`, `entity`, or `all_memories=true` is required.
    
    Note: Can only delete your own memories.
    Rate limit: 10 requests/minute.
    """
    if not any([memory_id, entity, all_memories]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: memory_id, entity, all_memories=true",
        )

    # SECURITY FIX: ALWAYS pass user_id to prevent IDOR (cross-user deletion)
    # This ensures users can only delete their own memories
    user_id = current_user.user_id

    try:
        result = await memory_service.forget(
            memory_id=memory_id,
            user_id=user_id,
            entity=entity,
        )
        
        # Audit log
        await audit_logger.log_memory_forget(
            user_id=current_user.user_id,
            resource_id=memory_id or f"user:{user_id}" if user_id else f"entity:{entity}",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=True,
        )

        # Record usage for metering (no-op if cloud disabled)
        await record_delete_usage(request, current_user.user_id)

        # Dispatch webhook event (no-op if webhooks disabled)
        await _dispatch_webhook(
            request,
            memory_deleted_event(
                user_id=current_user.user_id,
                memory_id=memory_id,
                deleted_count=result.deleted_count if hasattr(result, "deleted_count") else 1,
            ),
        )

        return result

    except Exception as e:
        await audit_logger.log_memory_forget(
            user_id=current_user.user_id,
            resource_id=memory_id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to forget memories: {str(e)}",
        )
