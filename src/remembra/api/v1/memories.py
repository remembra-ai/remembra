"""Memory CRUD endpoints – /api/v1/memories."""

import logging
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

log = structlog.get_logger(__name__)


def _is_memory_expired(memory: dict[str, Any]) -> bool:
    """
    Check if a memory has expired based on its expires_at timestamp.
    
    Args:
        memory: Memory dict with optional expires_at field
        
    Returns:
        True if memory has expired, False otherwise
    """
    expires_at = memory.get("expires_at")
    if not expires_at:
        return False
    
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    
    return datetime.utcnow() > expires_at.replace(tzinfo=None)

from remembra.auth.middleware import (
    CurrentUser,
    get_client_ip,
    has_permission,
    resolve_project_access,
)
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
    MemorySummary,
    RecallRequest,
    RecallResponse,
    StoreRequest,
    StoreResponse,
    UpdateRequest,
    UpdateResponse,
)
from remembra.security.audit import AuditLogger
from remembra.security.pii_detector import PIIDetector
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.webhooks.events import (
    WebhookEvent,
    memory_deleted_event,
    memory_recalled_event,
    memory_stored_event,
)

# Security: Logger for internal error details (never exposed to users)
_internal_log = structlog.get_logger("remembra.api.errors")
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


def get_pii_detector(request: Request) -> PIIDetector | None:
    """Dependency to get the PII detector from app state."""
    return getattr(request.app.state, "pii_detector", None)


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]
SanitizerDep = Annotated[ContentSanitizer, Depends(get_sanitizer)]
PIIDetectorDep = Annotated[PIIDetector | None, Depends(get_pii_detector)]


async def _dispatch_webhook(request: Request, event: WebhookEvent) -> None:
    """Fire-and-forget webhook dispatch. No-ops if webhooks are disabled."""
    manager = getattr(request.app.state, "webhook_manager", None)
    if manager is None:
        return
    try:
        await manager.dispatch(event)
    except Exception as exc:  # noqa: BLE001
        _webhook_log.warning("Webhook dispatch failed: %s", exc)


async def _broadcast_websocket(
    event_type: str,
    data: dict,
    project_id: str = "default",
) -> None:
    """Fire-and-forget WebSocket broadcast for real-time updates."""
    try:
        from remembra.api.v1.websocket import connection_manager

        await connection_manager.broadcast(
            event_type=event_type,
            data=data,
            namespace=project_id,
            project_id=project_id,
        )
    except Exception as exc:
        _webhook_log.warning("WebSocket broadcast failed: %s", exc)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[MemorySummary],
    summary="List stored memories for the current user",
)
@limiter.limit("60/minute")
async def list_memories(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    project_id: Annotated[
        str | None,
        Query(description="Filter by project. Omit to list memories across all projects."),
    ] = None,
) -> list[MemorySummary]:
    """List memories for dashboard browsing and pagination."""
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:recall required",
        )

    project_id = resolve_project_access(current_user, project_id)

    try:
        return await memory_service.list_memories(
            user_id=current_user.user_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        # Log full error internally for debugging (never expose to users)
        _internal_log.error(
            "list_memories_failed",
            user_id=current_user.user_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list memories. Please try again later.",
        ) from e


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
    pii_detector: PIIDetectorDep,
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
    # RBAC: Check permission
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )

    # Override user_id with authenticated user (security: prevent user spoofing)
    body.user_id = current_user.user_id
    body.project_id = resolve_project_access(current_user, body.project_id) or "default"

    # PII Detection (OWASP ASI06)
    pii_result = None
    if pii_detector:
        pii_result = pii_detector.scan(body.content, source="user_input")
        if pii_result.has_pii:
            if pii_result.blocked:
                # Block mode: reject content containing PII
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "PII_DETECTED",
                        "message": "Content contains sensitive information that cannot be stored",
                        "types": [m.type for m in pii_result.matches],
                    },
                )
            elif pii_result.redacted_content:
                # Redact mode: replace PII with placeholders
                body.content = pii_result.redacted_content

    # Sanitize content (XSS removal) and compute trust score
    sanitization = None
    if settings.sanitization_enabled:
        sanitization = sanitizer.analyze(body.content, source="user_input")
        # SECURITY: Use sanitized content (XSS stripped)
        body.content = sanitization.content

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

        # Broadcast to WebSocket clients for real-time updates
        await _broadcast_websocket(
            event_type="memory.created",
            data={
                "memory_id": result.id,
                "user_id": current_user.user_id,
                "facts": result.extracted_facts or [],
                "entities": [e.model_dump() if hasattr(e, "model_dump") else e for e in (result.entities or [])],
            },
            project_id=body.project_id or "default",
        )

        # Attach usage warning if set by cloud limit enforcement
        usage_warning = getattr(request.state, "usage_warning", None)
        if usage_warning is not None:
            result.usage_warning = usage_warning

        return result

    except ValueError as e:
        # ValueError is typically a validation error - safe to show to user
        error_msg = str(e)
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id="",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=error_msg,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        ) from e
    except Exception as e:
        # Log full error internally for debugging (never expose to users)
        _internal_log.error(
            "store_memory_failed",
            user_id=current_user.user_id,
            error=str(e),
            error_type=type(e).__name__,
        )
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
            detail="Failed to store memory. Please try again later.",
        ) from e


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
    pii_detector: PIIDetectorDep,
    current_user: CurrentUser,
) -> BatchStoreResponse:
    """
    Store up to 100 memories in a single request.

    Requires memory:store permission.

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
    # RBAC: Check permission
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )

    results: list[BatchStoreResult] = []
    succeeded = 0

    for i, item in enumerate(body.items):
        try:
            # Enforce authenticated user
            item.user_id = current_user.user_id
            item.project_id = resolve_project_access(current_user, item.project_id) or "default"

            # PII Detection for batch items
            if pii_detector:
                pii_result = pii_detector.scan(item.content, source="batch_input")
                if pii_result.has_pii:
                    if pii_result.blocked:
                        results.append(
                            BatchStoreResult(
                                index=i, success=False, error=f"PII_DETECTED: {[m.type for m in pii_result.matches]}"
                            )
                        )
                        continue
                    elif pii_result.redacted_content:
                        item.content = pii_result.redacted_content

            # SECURITY: XSS sanitization for batch items
            sanitization = sanitizer.analyze(item.content, source="batch_input")
            item.content = sanitization.content

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
    # RBAC: Check permission
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:recall required",
        )

    results: list[RecallResponse] = []

    for query in body.queries:
        # Enforce authenticated user
        query.user_id = current_user.user_id
        query.project_id = resolve_project_access(current_user, query.project_id) or "default"

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
    # RBAC: Check permission
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:recall required",
        )

    # Override user_id with authenticated user
    body.user_id = current_user.user_id
    body.project_id = resolve_project_access(current_user, body.project_id) or "default"

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
        # Log full error internally for debugging (never expose to users)
        _internal_log.error(
            "recall_memories_failed",
            user_id=current_user.user_id,
            query_length=len(body.query) if body.query else 0,
            error=str(e),
            error_type=type(e).__name__,
        )
        await audit_logger.log_memory_recall(
            user_id=current_user.user_id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to recall memories. Please try again later.",
        ) from e


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
    settings: SettingsDep,
) -> dict:
    """
    Retrieve a specific memory by its ID.

    Note: Can only access memories belonging to the authenticated user.
    
    **Strict Mode (v0.12):**
    When strict_mode=true, accessing an expired memory returns HTTP 410 GONE.
    """
    # Validate memory_id is a valid UUID format
    import uuid

    try:
        uuid.UUID(memory_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        ) from None

    # RBAC: Check permission
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:recall required",
        )

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

    # STRICT MODE: Check for expired memory reference (v0.12)
    if settings.strict_mode and _is_memory_expired(result):
        log.warning(
            "strict_mode_expired_get",
            memory_id=memory_id,
            user_id=current_user.user_id,
            expires_at=result.get("expires_at"),
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "MEMORY_EXPIRED",
                "message": f"Memory {memory_id} has expired. Re-acquire context via recall.",
                "memory_id": memory_id,
                "expires_at": result.get("expires_at"),
                "strict_mode": True,
            },
        )

    project_id = result.get("project_id")
    if project_id:
        resolve_project_access(current_user, project_id)

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
    sanitizer: SanitizerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
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

    **Strict Mode (v0.12):**
    When strict_mode=true in server config, attempting to update an expired
    memory returns HTTP 410 GONE. This forces agents to re-acquire context
    instead of silently creating orphan updates.

    Rate limit: 20 requests/minute.
    """
    # RBAC: Check permission (update requires store permission)
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )

    existing = await memory_service.get(memory_id)
    if not existing or existing.get("user_id") != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )
    
    # STRICT MODE: Check for expired memory reference (v0.12)
    # When enabled, writes to expired refs return 410 GONE to force
    # agents to re-acquire context instead of creating orphan updates
    if settings.strict_mode and _is_memory_expired(existing):
        log.warning(
            "strict_mode_expired_ref",
            memory_id=memory_id,
            user_id=current_user.user_id,
            expires_at=existing.get("expires_at"),
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "MEMORY_EXPIRED",
                "message": f"Memory {memory_id} has expired. Re-acquire context to continue.",
                "memory_id": memory_id,
                "expires_at": existing.get("expires_at"),
                "strict_mode": True,
            },
        )
    
    if existing.get("project_id"):
        resolve_project_access(current_user, existing["project_id"])

    # SECURITY: XSS sanitization for update content
    sanitized_content = body.content
    if body.content:
        sanitization = sanitizer.analyze(body.content, source="user_input")
        sanitized_content = sanitization.content

    try:
        result = await memory_service.update(
            memory_id=memory_id,
            user_id=current_user.user_id,
            new_content=sanitized_content,
            new_metadata=body.metadata,
        )
        from remembra.security.audit import AuditAction
        await audit_logger.log(
            user_id=current_user.user_id,
            action=AuditAction.MEMORY_UPDATE,
            resource_id=memory_id,
            success=True,
        )

        # Broadcast to WebSocket clients for real-time updates
        await _broadcast_websocket(
            event_type="memory.updated",
            data={
                "memory_id": memory_id,
                "user_id": current_user.user_id,
                "new_content": body.content[:100] if body.content else None,  # Truncate for privacy
            },
            project_id=existing.get("project_id") or "default",
        )

        return result
    except ValueError as ve:
        log.error("memory_update_value_error", error=str(ve), memory_id=memory_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        ) from None
    except Exception as e:
        from remembra.security.audit import AuditAction
        await audit_logger.log(
            user_id=current_user.user_id,
            action=AuditAction.MEMORY_UPDATE,
            resource_id=memory_id,
            success=False,
            error_message=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update memory",
        ) from e


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
    # RBAC: Check permission
    if not has_permission(current_user, "memory:delete"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:delete required",
        )

    try:
        project_id = resolve_project_access(current_user, None)
        deleted = await memory_service.cleanup_expired(
            user_id=current_user.user_id,
            project_id=project_id,
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
        # Log full error internally for debugging (never expose to users)
        _internal_log.error(
            "cleanup_expired_failed",
            user_id=current_user.user_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup expired memories. Please try again later.",
        ) from e


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
    memory_id: Annotated[str | None, Query(description="Delete a specific memory by ID")] = None,
    entity: Annotated[str | None, Query(description="Delete all memories about an entity")] = None,
    all_memories: Annotated[bool, Query(description="Delete all memories for the user")] = False,
) -> ForgetResponse:
    """
    Delete memories matching the given filter.

    At least one of `memory_id`, `entity`, or `all_memories=true` is required.

    Note: Can only delete your own memories.
    Rate limit: 10 requests/minute.
    """
    # RBAC: Check permission
    if not has_permission(current_user, "memory:delete"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:delete required",
        )

    if not any([memory_id, entity, all_memories]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: memory_id, entity, all_memories=true",
        )

    # SECURITY FIX: ALWAYS pass user_id to prevent IDOR (cross-user deletion)
    # This ensures users can only delete their own memories
    user_id = current_user.user_id

    if current_user.project_ids and (entity or all_memories):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Project-scoped API keys must delete by memory_id until project-scoped bulk delete is implemented."),
        )

    if memory_id:
        memory = await memory_service.get(memory_id)
        if not memory or memory.get("user_id") != current_user.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory {memory_id} not found",
            )
        if memory.get("project_id"):
            resolve_project_access(current_user, memory["project_id"])

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

        # Broadcast to WebSocket clients for real-time updates
        await _broadcast_websocket(
            event_type="memory.deleted",
            data={
                "memory_id": memory_id,
                "user_id": current_user.user_id,
                "deleted_count": result.deleted_count if hasattr(result, "deleted_count") else 1,
                "entity": entity,
            },
            project_id="default",
        )

        return result

    except Exception as e:
        # Log full error internally for debugging (never expose to users)
        _internal_log.error(
            "forget_memories_failed",
            user_id=current_user.user_id,
            memory_id=memory_id,
            error=str(e),
            error_type=type(e).__name__,
        )
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
            detail="Failed to forget memories. Please try again later.",
        ) from e
