"""Ingestion endpoints - import external data sources into memories."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, get_client_ip
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter
from remembra.ingestion.changelog import ChangelogParser, ChangelogRelease
from remembra.models.memory import (
    ConversationIngestRequest,
    ConversationIngestResponse,
    StoreRequest,
)
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.conversation_ingest import ConversationIngestService
from remembra.services.memory import MemoryService

router = APIRouter(prefix="/ingest", tags=["ingestion"])


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    return request.app.state.memory_service


def get_audit_logger(request: Request) -> AuditLogger:
    """Dependency to get the audit logger from app state."""
    return request.app.state.audit_logger


def get_conversation_ingest(request: Request) -> ConversationIngestService:
    """Dependency to get the conversation ingest service from app state."""
    return request.app.state.conversation_ingest


def get_sanitizer(request: Request) -> ContentSanitizer:
    """Dependency to get the content sanitizer from app state."""
    return request.app.state.sanitizer


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]
ConversationIngestDep = Annotated[ConversationIngestService, Depends(get_conversation_ingest)]
SanitizerDep = Annotated[ContentSanitizer, Depends(get_sanitizer)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class ChangelogIngestRequest(BaseModel):
    """Request to ingest a changelog."""
    
    content: str | None = Field(
        default=None,
        description="Raw markdown content of the changelog",
    )
    file_path: str | None = Field(
        default=None,
        description="Path to a CHANGELOG.md file (server-side)",
    )
    project_id: str = Field(
        default="default",
        description="Project namespace for stored memories",
    )
    project_name: str | None = Field(
        default=None,
        description="Human-readable project name for context",
    )
    max_releases: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of releases to ingest",
    )
    skip_unreleased: bool = Field(
        default=True,
        description="Skip [Unreleased] section",
    )


class ChangelogIngestResponse(BaseModel):
    """Response from changelog ingestion."""
    
    releases_parsed: int
    memories_stored: int
    memory_ids: list[str]
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Changelog Ingestion
# ---------------------------------------------------------------------------


@router.post(
    "/changelog",
    response_model=ChangelogIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest project history from a CHANGELOG.md",
)
@limiter.limit("10/minute")
async def ingest_changelog(
    request: Request,
    body: ChangelogIngestRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> ChangelogIngestResponse:
    """
    Parse a CHANGELOG.md and store each release as a memory.
    
    Supports:
    - Keep a Changelog format (https://keepachangelog.com/)
    - Most markdown changelogs with ## version headers
    
    Each release becomes a memory with version/date metadata,
    making project history searchable and recallable.
    
    **Usage (content):**
    ```json
    {
      "content": "## [1.0.0] - 2024-01-15\\n### Added\\n- Feature X",
      "project_name": "my-project"
    }
    ```
    
    **Usage (file path):**
    ```json
    {
      "file_path": "/path/to/CHANGELOG.md",
      "project_name": "my-project"
    }
    ```
    
    Rate limit: 10 requests/minute.
    """
    if not body.content and not body.file_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either 'content' or 'file_path' must be provided",
        )
    
    parser = ChangelogParser()
    releases: list[ChangelogRelease] = []
    errors: list[str] = []
    
    # Parse changelog
    try:
        if body.file_path:
            releases = parser.parse_file(body.file_path)
        else:
            releases = parser.parse(body.content)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Changelog file not found: {body.file_path}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse changelog: {str(e)}",
        )
    
    if not releases:
        return ChangelogIngestResponse(
            releases_parsed=0,
            memories_stored=0,
            memory_ids=[],
            errors=["No releases found in changelog"],
        )
    
    # Filter releases
    if body.skip_unreleased:
        releases = [r for r in releases if r.version.lower() != "unreleased"]
    
    releases = releases[:body.max_releases]
    
    # Store each release as a memory
    memory_ids: list[str] = []
    
    for release in releases:
        try:
            # Build content string
            content = release.to_memory_content()
            if body.project_name:
                content = f"Project {body.project_name} - {content}"
            
            # Build metadata
            metadata = release.to_metadata()
            if body.project_name:
                metadata["project_name"] = body.project_name
            
            # Store via memory service
            store_request = StoreRequest(
                user_id=current_user.user_id,
                content=content,
                project_id=body.project_id,
                metadata=metadata,
            )
            
            result = await memory_service.store(
                store_request,
                source="changelog_ingestion",
                trust_score=1.0,  # Changelogs are trusted
            )
            
            if result.id:
                memory_ids.append(result.id)
                
        except Exception as e:
            errors.append(f"Failed to store release {release.version}: {str(e)}")
    
    # Audit log
    await audit_logger.log_memory_store(
        user_id=current_user.user_id,
        memory_id=f"changelog:{len(memory_ids)}_releases",
        api_key_id=current_user.api_key_id,
        ip_address=get_client_ip(request),
        success=len(memory_ids) > 0,
        error="; ".join(errors) if errors else None,
    )
    
    return ChangelogIngestResponse(
        releases_parsed=len(releases),
        memories_stored=len(memory_ids),
        memory_ids=memory_ids,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Conversation Ingestion (Phase 1 - Critical Feature)
# ---------------------------------------------------------------------------


@router.post(
    "/conversation",
    response_model=ConversationIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a conversation and extract memories automatically",
)
@limiter.limit("20/minute")
async def ingest_conversation(
    request: Request,
    body: ConversationIngestRequest,
    conversation_ingest: ConversationIngestDep,
    audit_logger: AuditLoggerDep,
    sanitizer: SanitizerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> ConversationIngestResponse:
    """
    Parse a conversation and automatically extract memorable facts.
    
    This is the primary ingestion endpoint for AI agents. It accepts a list
    of messages and intelligently extracts facts worth remembering long-term.
    
    **Features:**
    - Automatic fact extraction with importance scoring
    - Entity extraction (people, organizations, locations, etc.)
    - Deduplication against existing memories
    - Speaker attribution
    - Pronoun resolution using conversation context
    
    **Modes:**
    - `options.infer=true` (default): Full extraction pipeline
    - `options.infer=false`: Store raw messages without extraction
    - `options.store=false`: Dry run - returns extraction without storing
    
    **Example:**
    ```json
    {
      "messages": [
        {"role": "user", "content": "My wife Suzan and I are planning a trip to Japan"},
        {"role": "assistant", "content": "That sounds exciting! When are you planning to go?"},
        {"role": "user", "content": "We're thinking April next year"}
      ],
      "options": {"min_importance": 0.5}
    }
    ```
    
    **Response includes:**
    - `facts`: Extracted facts with importance scores and storage status
    - `entities`: People, organizations, locations found
    - `deduped`: Facts that matched existing memories
    - `stats`: Processing statistics
    
    Rate limit: 20 requests/minute.
    """
    # Override user_id with authenticated user (security: prevent spoofing)
    body.user_id = current_user.user_id
    
    # Sanitize all message content
    if settings.sanitization_enabled:
        for msg in body.messages:
            sanitization = sanitizer.analyze(msg.content, source="conversation_ingest")
            if sanitization.trust_score < 0.3:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Message content failed security check: {sanitization.warnings}",
                )
    
    try:
        # Process conversation through the ingest service
        result = await conversation_ingest.ingest(body)
        
        # Audit log
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id=f"conversation:{result.stats.facts_stored}_facts",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=result.status in ["ok", "partial"],
            error=None if result.status == "ok" else "Partial processing",
        )
        
        # Dispatch webhook if configured
        webhook_manager = getattr(request.app.state, "webhook_manager", None)
        if webhook_manager and result.stats.facts_stored > 0:
            try:
                from remembra.webhooks.events import WebhookEvent
                await webhook_manager.dispatch(WebhookEvent(
                    event_type="conversation_ingested",
                    user_id=current_user.user_id,
                    data={
                        "session_id": result.session_id,
                        "facts_stored": result.stats.facts_stored,
                        "entities_found": result.stats.entities_found,
                        "project_id": body.project_id,
                    },
                ))
            except Exception:
                pass  # Don't fail the request on webhook error
        
        return result
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        # Log error and return partial result if possible
        await audit_logger.log_memory_store(
            user_id=current_user.user_id,
            memory_id="conversation:error",
            api_key_id=current_user.api_key_id,
            ip_address=get_client_ip(request),
            success=False,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conversation ingestion failed: {str(e)}",
        )
