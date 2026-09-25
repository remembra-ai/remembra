"""Memory CRUD endpoints – /api/v1/memories."""

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from remembra.auth.middleware import (
    CurrentUser,
    get_client_ip,
    has_permission,
    resolve_project_access,
)
from remembra.cloud.limits import (
    EnforceRecallLimit,
    EnforceStoreLimit,
    enforce_recall_quota,
    enforce_store_quota,
    record_delete_usage,
    record_recall_usage,
    record_store_usage,
)
from remembra.config import Settings, get_settings
from remembra.core.http_errors import embedding_http_exception
from remembra.core.limiter import limiter
from remembra.core.time import utcnow
from remembra.models.memory import (
    BatchRecallRequest,
    BatchRecallResponse,
    BatchStoreRequest,
    BatchStoreResponse,
    BatchStoreResult,
    FeedbackRequest,
    FeedbackResponse,
    ForgetResponse,
    ImportanceRequest,
    MemorySummary,
    RecallRequest,
    RecallResponse,
    SalienceResponse,
    StoreRequest,
    StoreResponse,
    SupersedeRequest,
    SupersedeResponse,
    UpdateRequest,
    UpdateResponse,
)
from remembra.security.audit import AuditLogger
from remembra.security.content_policy import prepare_content
from remembra.security.pii_detector import PIIDetector
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.agent_session import MemoryTypePolicyError, apply_memory_type_policy
from remembra.services.memory import MemoryService
from remembra.services.relay import strip_reserved_metadata
from remembra.storage.embeddings import EmbeddingProviderError
from remembra.webhooks.events import (
    WebhookEvent,
    memory_deleted_event,
    memory_recalled_event,
    memory_stored_event,
)

log = structlog.get_logger(__name__)

# Security: Logger for internal error details (never exposed to users)
_internal_log = structlog.get_logger("remembra.api.errors")
_webhook_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotency (ING-18) — persisted in SQLite with in-flight state, so a retry
# after a client timeout (or a server restart) replays the original response
# instead of storing twice, and a concurrent duplicate gets 409.
# ---------------------------------------------------------------------------


def _idempotency_request_hash(body: StoreRequest) -> str:
    return hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest()


async def _release_idempotency(memory_service: MemoryService, user_id: str, key: str | None) -> None:
    """Free an in-flight key after a failed store so the client can retry."""
    if not key:
        return
    try:
        await memory_service.db.idempotency_release(user_id, key)
    except Exception as exc:  # noqa: BLE001 — never mask the original error
        log.warning("idempotency_release_failed", error=str(exc))


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

    return utcnow() > expires_at.replace(tzinfo=None)


router = APIRouter(prefix="/memories", tags=["memories"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    service: MemoryService = request.app.state.memory_service
    return service


def get_audit_logger(request: Request) -> AuditLogger:
    """Dependency to get the audit logger from app state."""
    logger: AuditLogger = request.app.state.audit_logger
    return logger


def get_sanitizer(request: Request) -> ContentSanitizer:
    """Dependency to get the content sanitizer from app state."""
    sanitizer: ContentSanitizer = request.app.state.sanitizer
    return sanitizer


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
    data: dict[str, Any],
    project_id: str = "default",
) -> None:
    """Fire-and-forget WebSocket broadcast, delivered only to the owning user's sockets."""
    try:
        from remembra.api.v1.websocket import connection_manager

        await connection_manager.broadcast(
            event_type=event_type,
            data=data,
            user_id=data.get("user_id"),
            project_id=project_id,
        )
    except Exception as exc:
        _webhook_log.warning("WebSocket broadcast failed: %s", exc)


async def _apply_trust_policy(
    memory_service: MemoryService,
    result: RecallResponse,
    include_low_trust: bool,
) -> RecallResponse:
    """Surface each memory's stored trust score; withhold low-trust memories (SEC-13).

    Content that tripped the prompt-injection sanitizer when it was written is
    kept out of agent-facing recall unless the caller explicitly opts in with
    ``include_low_trust=true``. When anything is withheld the context string is
    rebuilt from the remaining memories so the text is not leaked there either.
    """
    ids = [m.id for m in result.memories]
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    cursor = await memory_service.db.conn.execute(
        f"SELECT id, trust_score FROM memories WHERE id IN ({placeholders})",
        ids,
    )
    trust = {row[0]: row[1] for row in await cursor.fetchall()}
    threshold = get_settings().trust_score_threshold

    kept = []
    for memory in result.memories:
        score = trust.get(memory.id)
        memory.trust_score = float(score) if score is not None else None
        if not include_low_trust and memory.trust_score is not None and memory.trust_score < threshold:
            continue
        kept.append(memory)
    if len(kept) != len(result.memories):
        _internal_log.info("recall_low_trust_withheld", withheld=len(result.memories) - len(kept))
        result.memories = kept
        result.context = "\n\n".join(m.content for m in kept)
    return result


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def _client_metadata(user: Any, metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Client-supplied metadata as it may be stored.

    Relay-only keys (``relay``, ``relay_key``) are dropped: a handoff's relay
    block is written by ``POST /session/close`` alone, so it cannot be forged
    or edited here. With an agent-scoped key, ``agent_id`` is the key's agent
    (attribution comes from the key, not the request).
    """
    cleaned = strip_reserved_metadata(metadata)
    agent = getattr(user, "agent_id", None)
    if agent:
        cleaned = {**(cleaned or {}), "agent_id": agent}
    return cleaned


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
) -> list[dict[str, Any]]:
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
    body.metadata = _client_metadata(current_user, body.metadata) or {}

    # Agent memory-type hygiene: checkpoint TTL, atomic handoff, status via upsert (AGT-5)
    try:
        apply_memory_type_policy(body, settings.checkpoint_default_ttl)
    except MemoryTypePolicyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

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

    # Idempotency — claim the key durably before doing any work (after
    # validation/PII/sanitization, so a rejected request never holds a key)
    idem_key = (request.headers.get("Idempotency-Key") or "").strip()[:200] or None
    if idem_key:
        state, cached = await memory_service.db.idempotency_claim(
            current_user.user_id,
            idem_key,
            _idempotency_request_hash(body),
            ttl_seconds=settings.idempotency_ttl_hours * 3600,
            inflight_timeout_seconds=settings.idempotency_inflight_timeout_seconds,
        )
        if state == "done" and cached:
            log.info("idempotency_replay", key=idem_key)
            return StoreResponse.model_validate_json(cached)
        if state == "in_flight":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A request with this Idempotency-Key is still being processed. Retry shortly.",
                headers={"Retry-After": "2"},
            )
        if state == "mismatch":
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key was already used with a different request body.",
            )

    try:
        result = await memory_service.store(
            body,
            source="user_input",
            trust_score=sanitization.trust_score if sanitization else 1.0,
            checksum=sanitization.checksum if sanitization else None,
            skip_extraction=body.skip_extraction,
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

        # Only announce memories that were actually created (ING-24): a
        # duplicate store created nothing.
        if result.status in ("stored", "pending"):
            await _dispatch_webhook(
                request,
                memory_stored_event(
                    user_id=current_user.user_id,
                    memory_id=result.id,
                    extracted_facts=result.extracted_facts,
                    entities=[e.canonical_name for e in result.entities],
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
                    "entities": [e.model_dump() for e in result.entities],
                },
                project_id=body.project_id or "default",
            )

        # Attach usage warning if set by cloud limit enforcement
        usage_warning = getattr(request.state, "usage_warning", None)
        if usage_warning is not None:
            result.usage_warning = usage_warning

        if idem_key:
            await memory_service.db.idempotency_complete(current_user.user_id, idem_key, result.model_dump_json())
            idem_key = None

        return result

    except ValueError as e:
        await _release_idempotency(memory_service, current_user.user_id, idem_key)
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
    except EmbeddingProviderError as e:
        await _release_idempotency(memory_service, current_user.user_id, idem_key)
        # Upstream embedding provider failed — surface an honest status
        # instead of collapsing it into a generic 500.
        _internal_log.error(
            "store_memory_embedding_upstream_error",
            user_id=current_user.user_id,
            upstream_status=e.status_code,
            kind=e.kind.value,
        )
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id="",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise embedding_http_exception(e, "store") from e
    except Exception as e:
        await _release_idempotency(memory_service, current_user.user_id, idem_key)
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
            headers={"Retry-After": "2"},
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
@limiter.limit("30/minute")
async def batch_store(
    request: Request,
    body: BatchStoreRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    sanitizer: SanitizerDep,
    pii_detector: PIIDetectorDep,
    current_user: CurrentUser,
    settings: SettingsDep,
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

    Items are stored with bounded concurrency (``batch_store_concurrency``).
    Rate limit: 30 requests/minute.
    """
    # RBAC: Check permission
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )

    # BatchStoreRequest.resolve_items_alias validates that items is non-empty
    # (raising otherwise), so it is always populated by the time we get here.
    assert body.items is not None, "items is guaranteed non-None by request validation"

    items = body.items
    semaphore = asyncio.Semaphore(settings.batch_store_concurrency)

    # Plan limits apply to the whole batch, not just single stores (SEC-11).
    await enforce_store_quota(request, current_user.user_id, len(items))

    async def store_item(i: int, item: StoreRequest) -> BatchStoreResult:
        # Enforce authenticated user
        item.user_id = current_user.user_id
        try:
            item.project_id = resolve_project_access(current_user, item.project_id) or "default"
            item.metadata = _client_metadata(current_user, item.metadata) or {}
        except HTTPException as e:
            return BatchStoreResult(index=i, success=False, error=str(e.detail))

        # Same PII + injection policy as single store; trust score + checksum are
        # persisted like single stores (SEC-13, ING-21).
        prepared = prepare_content(
            request.app.state,
            item.content,
            source="batch_input",
            sanitization_enabled=settings.sanitization_enabled,
        )
        if prepared.blocked:
            return BatchStoreResult(index=i, success=False, error=f"PII_DETECTED: {prepared.blocked_pii_types}")
        item.content = prepared.content

        async with semaphore:
            try:
                resp = await memory_service.store(
                    item,
                    source="user_input",
                    trust_score=prepared.trust_score,
                    checksum=prepared.checksum,
                    skip_extraction=body.skip_extraction or item.skip_extraction,
                )
                return BatchStoreResult(index=i, success=True, response=resp)
            except ValueError as e:
                return BatchStoreResult(index=i, success=False, error=str(e))
            except EmbeddingProviderError as e:
                _internal_log.error("batch_store_item_embedding_error", index=i, upstream_status=e.status_code)
                return BatchStoreResult(index=i, success=False, error="Embedding provider error; item not stored")
            except Exception as e:
                # Never echo internal exception text to the client (ING-21).
                _internal_log.error("batch_store_item_failed", index=i, error=str(e), error_type=type(e).__name__)
                return BatchStoreResult(index=i, success=False, error="Failed to store item")

    results = list(await asyncio.gather(*(store_item(i, item) for i, item in enumerate(items))))
    succeeded = sum(1 for r in results if r.success)

    await record_store_usage(request, current_user.user_id, succeeded)

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
# Bulk Import (Fast Path)
# ---------------------------------------------------------------------------


@router.post(
    "/bulk",
    status_code=status.HTTP_201_CREATED,
    summary="Fast bulk import for pre-structured data",
)
@limiter.limit("10/minute")
async def bulk_import(
    request: Request,
    body: BatchStoreRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """
    Fast bulk import optimized for pre-structured data.

    Unlike /batch which processes items individually, /bulk:
    - Embeds ALL items in a single OpenAI call
    - Bulk upserts to Qdrant in one operation
    - Bulk inserts to SQLite in one transaction

    This is 10-50x faster than /batch for large imports.

    **Use when:**
    - Importing pre-structured data (trading summaries, logs, etc.)
    - Data is known-good and doesn't need deduplication
    - Speed is critical

    **Skips:** Fact extraction, consolidation, conflict detection.

    **Request (with server-side embedding):**
    ```json
    {
      "items": [
        {"content": "NQ summary...", "metadata": {"symbol": "NQ"}},
        {"content": "ES summary...", "metadata": {"symbol": "ES"}}
      ]
    }
    ```

    **Request (with pre-computed embeddings - FASTEST):**
    ```json
    {
      "items": [{"content": "...", "metadata": {...}}, ...],
      "embeddings": [[0.1, 0.2, ...], [0.3, 0.4, ...]]
    }
    ```

    When embeddings are provided, OpenAI calls are skipped entirely.
    Generate embeddings client-side using text-embedding-3-small (1536 dimensions).

    Rate limit: 10 requests/minute (100 items/request = 1000 items/minute max)
    """
    # RBAC: Check permission
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )

    # BatchStoreRequest.resolve_items_alias validates that items is non-empty
    # (raising otherwise), so it is always populated by the time we get here.
    assert body.items is not None, "items is guaranteed non-None by request validation"

    # Resolve project
    project_id = resolve_project_access(current_user, body.items[0].project_id if body.items else None) or "default"

    # Plan limits + PII/injection policy apply to the fast path too (SEC-11/SEC-13).
    await enforce_store_quota(request, current_user.user_id, len(body.items))
    items = []
    embeddings: list[list[float]] | None = [] if body.embeddings is not None else None
    policy_errors: list[dict[str, Any]] = []
    for i, item in enumerate(body.items):
        item.metadata = _client_metadata(current_user, item.metadata) or {}
        prepared = prepare_content(
            request.app.state,
            item.content,
            source="bulk_import",
            sanitization_enabled=get_settings().sanitization_enabled,
        )
        if prepared.blocked:
            policy_errors.append({"index": i, "error": f"PII_DETECTED: {prepared.blocked_pii_types}"})
            continue
        item.content = prepared.content
        items.append(item)
        if embeddings is not None and body.embeddings is not None:
            embeddings.append(body.embeddings[i])
    if not items:
        return {"status": "ok", "stored": 0, "errors": policy_errors}

    try:
        result = await memory_service.bulk_import(
            items=items,
            user_id=current_user.user_id,
            project_id=project_id,
            embeddings=embeddings,
        )
        await record_store_usage(request, current_user.user_id, int(result.get("stored", 0)))

        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id=f"bulk:{result.get('stored', 0)}",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=True,
        )

        return {
            "status": "ok",
            "stored": result.get("stored", 0),
            "errors": policy_errors + list(result.get("errors", [])),
        }

    except Exception as e:
        log.error("bulk_import_failed", error=str(e), user_id=current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bulk import failed.",
        ) from e


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

    # Every query counts against the plan's recall limit (SEC-11).
    await enforce_recall_quota(request, current_user.user_id, len(body.queries))

    for query in body.queries:
        # Enforce authenticated user
        query.user_id = current_user.user_id
        # Recall: when client doesn't pass a project_id, let None flow through so
        # the service performs cross-project recall across all of the user's data.
        # Restricted API keys still resolve to their allowed project(s) via
        # resolve_project_access (may raise 403 or pin to a specific project).
        query.project_id = resolve_project_access(current_user, query.project_id)

        resp = await memory_service.recall(query)
        results.append(await _apply_trust_policy(memory_service, resp, include_low_trust=False))

    await record_recall_usage(request, current_user.user_id, len(body.queries))

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
    include_low_trust: Annotated[
        bool, Query(description="Include memories flagged as possible prompt injection (trust below threshold)")
    ] = False,
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
    # Recall: when client doesn't pass a project_id, let None flow through so
    # the service performs cross-project recall across all of the user's data.
    # Restricted API keys still resolve to their allowed project(s) via
    # resolve_project_access (may raise 403 or pin to a specific project).
    body.project_id = resolve_project_access(current_user, body.project_id)

    try:
        result = await memory_service.recall(body)
        result = await _apply_trust_policy(memory_service, result, include_low_trust)

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
                query=body.query or "",
                result_count=len(result.memories) if result.memories else 0,
                project_id=body.project_id or "default",
            ),
        )

        # Attach usage warning if set by cloud limit enforcement
        usage_warning = getattr(request.state, "usage_warning", None)
        if usage_warning is not None:
            result.usage_warning = usage_warning

        return result

    except EmbeddingProviderError as e:
        # Query embedding failed upstream — return an honest status.
        _internal_log.error(
            "recall_embedding_upstream_error",
            user_id=current_user.user_id,
            upstream_status=e.status_code,
            kind=e.kind.value,
        )
        await audit_logger.log_memory_recall(
            user_id=current_user.user_id,
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise embedding_http_exception(e, "recall") from e
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
) -> dict[str, Any]:
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

    # SEC-23: never hand back stored credentials (rows predating redaction).
    from remembra.security.secrets import scrub_memory_record

    return scrub_memory_record(result)


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
            new_metadata=_client_metadata(current_user, body.metadata),
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
# Supersede (v0.13 - Explicit Belief Updates)
# ---------------------------------------------------------------------------


@router.post(
    "/{memory_id}/supersede",
    response_model=SupersedeResponse,
    summary="Supersede a memory with new information",
)
@limiter.limit("20/minute")
async def supersede_memory(
    request: Request,
    memory_id: str,
    body: SupersedeRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    sanitizer: SanitizerDep,
    current_user: CurrentUser,
) -> SupersedeResponse:
    """
    Explicitly supersede a memory with new information.

    This is for when you know that old information is now outdated
    and want to create a clear audit trail of the belief update.

    - **new_content**: The updated information
    - **reason**: Why this supersession is happening

    The old memory is NOT deleted - it's marked as superseded.
    This creates a full audit trail of how beliefs evolved over time.

    Example:
    ```json
    {
        "new_content": "Alice now works at NewCorp",
        "reason": "She changed jobs in March 2026"
    }
    ```

    Rate limit: 20 requests/minute.
    """
    # RBAC: Check permission (supersede requires store permission)
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:store required",
        )
    # Ownership + project scope of the memory being superseded (SEC-14).
    await _require_owned_memory(memory_service, memory_id, current_user)

    # SECURITY: XSS sanitization for new content
    sanitization = sanitizer.analyze(body.new_content, source="user_input")
    sanitized_content = sanitization.content

    try:
        result = await memory_service.supersede(
            old_memory_id=memory_id,
            user_id=current_user.user_id,
            new_content=sanitized_content,
            reason=body.reason,
            metadata=_client_metadata(current_user, body.metadata),
        )

        # Audit log
        from remembra.security.audit import AuditAction

        await audit_logger.log(
            user_id=current_user.user_id,
            action=AuditAction.MEMORY_UPDATE,  # Or create MEMORY_SUPERSEDE
            resource_id=memory_id,
            success=True,
        )

        # Broadcast to WebSocket
        await _broadcast_websocket(
            event_type="memory.superseded",
            data={
                "old_memory_id": memory_id,
                "new_memory_id": result.new_memory_id,
                "user_id": current_user.user_id,
                "reason": body.reason,
            },
            project_id="default",
        )

        return result

    except ValueError as ve:
        log.error("supersede_memory_value_error", error=str(ve), memory_id=memory_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        ) from None
    except Exception as e:
        log.error("supersede_memory_error", error=str(e), memory_id=memory_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to supersede memory",
        ) from e


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


@router.post(
    "/{memory_id}/feedback",
    response_model=FeedbackResponse,
    summary="Submit helpful/unhelpful feedback for a recalled memory",
)
@limiter.limit("30/minute")
async def submit_feedback(
    request: Request,
    memory_id: str,
    body: FeedbackRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> FeedbackResponse:
    """
    Record whether a recalled memory was useful.

    - **signal**: "helpful" or "unhelpful"
    - **comment**: Optional free-text comment
    - **query**: The query that surfaced this memory

    Rate limit: 30 requests/minute.
    """
    import uuid

    # Verify memory exists and belongs to user
    memory = await memory_service.get(memory_id)
    if not memory or memory.get("user_id") != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )

    feedback_id = str(uuid.uuid4())
    await memory_service.db.save_feedback(
        feedback_id=feedback_id,
        memory_id=memory_id,
        user_id=current_user.user_id,
        signal=body.signal,
        comment=body.comment,
        query=body.query,
    )

    log.info(
        "feedback_recorded",
        memory_id=memory_id,
        signal=body.signal,
        user_id=current_user.user_id,
    )

    return FeedbackResponse(memory_id=memory_id, signal=body.signal)


# ---------------------------------------------------------------------------
# Salience: pin / unpin / importance (protect important memories from decay)
# ---------------------------------------------------------------------------


async def _require_owned_memory(
    memory_service: Any,
    memory_id: str,
    user: Any,
    permission: str = "memory:store",
) -> dict[str, Any]:
    """Fetch a memory; 403 without ``permission``, 404 unless owned, 403 outside the key's projects."""
    if not has_permission(user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {permission} required",
        )
    memory = await memory_service.get(memory_id)
    if not memory or memory.get("user_id") != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )
    resolve_project_access(user, memory.get("project_id") or "default")
    result: dict[str, Any] = memory
    return result


@router.post("/{memory_id}/pin", response_model=SalienceResponse, summary="Pin a memory (never decays)")
@limiter.limit("60/minute")
async def pin_memory(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> SalienceResponse:
    """Pin a memory so it is never pruned by temporal decay or TTL expiration."""
    await _require_owned_memory(memory_service, memory_id, current_user)
    await memory_service.db.set_memory_pin(memory_id, current_user.user_id, True)
    log.info("memory_pinned", memory_id=memory_id, user_id=current_user.user_id)
    return SalienceResponse(memory_id=memory_id, pinned=True)


@router.post("/{memory_id}/unpin", response_model=SalienceResponse, summary="Unpin a memory")
@limiter.limit("60/minute")
async def unpin_memory(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> SalienceResponse:
    """Remove decay protection from a previously pinned memory."""
    await _require_owned_memory(memory_service, memory_id, current_user)
    await memory_service.db.set_memory_pin(memory_id, current_user.user_id, False)
    log.info("memory_unpinned", memory_id=memory_id, user_id=current_user.user_id)
    return SalienceResponse(memory_id=memory_id, pinned=False)


@router.patch("/{memory_id}/importance", response_model=SalienceResponse, summary="Set memory importance")
@limiter.limit("60/minute")
async def set_memory_importance(
    request: Request,
    memory_id: str,
    body: ImportanceRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> SalienceResponse:
    """Set a memory's importance (salience) in [0,1]. Higher importance decays slower."""
    await _require_owned_memory(memory_service, memory_id, current_user)
    await memory_service.db.set_memory_importance(memory_id, current_user.user_id, body.importance)
    log.info("memory_importance_set", memory_id=memory_id, importance=body.importance, user_id=current_user.user_id)
    return SalienceResponse(memory_id=memory_id, importance=body.importance)


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
) -> dict[str, Any]:
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
    project_id: Annotated[str | None, Query(description="Delete all memories in a specific project")] = None,
) -> ForgetResponse:
    """
    Delete memories matching the given filter.

    At least one of `memory_id`, `entity`, `all_memories=true`, or `project_id` is required.

    For project-scoped API keys:
    - Use `project_id` to delete all memories in your project
    - Or delete by `memory_id` for individual deletions

    Note: Can only delete your own memories.
    Rate limit: 10 requests/minute.
    """
    # RBAC: Check permission
    if not has_permission(current_user, "memory:delete"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: memory:delete required",
        )

    if not any([memory_id, entity, all_memories, project_id]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: memory_id, entity, all_memories=true, project_id",
        )

    # SECURITY FIX: ALWAYS pass user_id to prevent IDOR (cross-user deletion)
    # This ensures users can only delete their own memories
    user_id = current_user.user_id

    # For project-scoped API keys doing bulk operations
    if current_user.project_ids:
        if entity or all_memories:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project-scoped API keys cannot use entity or all_memories. Use project_id for bulk delete.",
            )
        # If project_id provided, verify it's in the allowed projects
        if project_id and project_id not in current_user.project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key does not have access to project: {project_id}",
            )
        # If no project_id but we have project_ids, use the first one (single-project key)
        if not project_id and not memory_id and len(current_user.project_ids) == 1:
            project_id = current_user.project_ids[0]

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
            project_id=project_id,
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
