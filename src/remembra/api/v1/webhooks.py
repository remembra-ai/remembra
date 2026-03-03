"""Webhook management endpoints – /api/v1/webhooks."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.webhooks.events import ALL_EVENT_TYPES
from remembra.webhooks.manager import WebhookManager

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_webhook_manager(request: Request) -> WebhookManager:
    manager = getattr(request.app.state, "webhook_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhooks are not enabled on this instance.",
        )
    return manager


WebhookManagerDep = Annotated[WebhookManager, Depends(get_webhook_manager)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RegisterWebhookRequest(BaseModel):
    url: str = Field(description="Target URL for HTTP POST webhook deliveries")
    events: list[str] = Field(
        description=f"Event types to subscribe to: {', '.join(ALL_EVENT_TYPES)} or '*' for all"
    )
    secret: str | None = Field(
        None,
        description="Optional signing secret for HMAC-SHA256 payload verification",
    )


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    active: bool
    has_secret: bool = False
    created_at: str
    updated_at: str | None = None


class WebhookListResponse(BaseModel):
    webhooks: list[dict[str, Any]]
    total: int


class UpdateWebhookRequest(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    active: bool | None = None


class DeliveryResponse(BaseModel):
    deliveries: list[dict[str, Any]]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook",
)
@limiter.limit("10/minute")
async def register_webhook(
    request: Request,
    body: RegisterWebhookRequest,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Register a new webhook endpoint to receive event notifications."""
    try:
        result = await manager.register(
            user_id=current_user.user_id,
            url=body.url,
            events=body.events,
            secret=body.secret,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "",
    response_model=WebhookListResponse,
    summary="List webhooks",
)
@limiter.limit("30/minute")
async def list_webhooks(
    request: Request,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
) -> WebhookListResponse:
    """List all registered webhooks for the current user."""
    webhooks = await manager.list_webhooks(current_user.user_id)
    return WebhookListResponse(webhooks=webhooks, total=len(webhooks))


@router.get(
    "/{webhook_id}",
    summary="Get webhook details",
)
@limiter.limit("30/minute")
async def get_webhook(
    request: Request,
    webhook_id: str,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Get details of a specific webhook."""
    result = await manager.get_webhook(webhook_id, current_user.user_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook {webhook_id} not found",
        )
    return result


@router.patch(
    "/{webhook_id}",
    summary="Update a webhook",
)
@limiter.limit("10/minute")
async def update_webhook(
    request: Request,
    webhook_id: str,
    body: UpdateWebhookRequest,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Update a webhook registration."""
    try:
        result = await manager.update_webhook(
            webhook_id=webhook_id,
            user_id=current_user.user_id,
            url=body.url,
            events=body.events,
            active=body.active,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook {webhook_id} not found",
        )
    return result


@router.delete(
    "/{webhook_id}",
    summary="Delete a webhook",
)
@limiter.limit("10/minute")
async def delete_webhook(
    request: Request,
    webhook_id: str,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
) -> dict[str, str]:
    """Delete a webhook registration."""
    deleted = await manager.delete_webhook(webhook_id, current_user.user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook {webhook_id} not found",
        )
    return {"status": "deleted", "id": webhook_id}


@router.get(
    "/{webhook_id}/deliveries",
    response_model=DeliveryResponse,
    summary="Get webhook delivery history",
)
@limiter.limit("30/minute")
async def get_deliveries(
    request: Request,
    webhook_id: str,
    manager: WebhookManagerDep,
    current_user: CurrentUser,
    limit: int = 50,
) -> DeliveryResponse:
    """Get recent delivery attempts for a webhook."""
    deliveries = await manager.get_deliveries(
        webhook_id=webhook_id,
        user_id=current_user.user_id,
        limit=min(limit, 100),
    )
    return DeliveryResponse(deliveries=deliveries, total=len(deliveries))


@router.get(
    "/events/types",
    summary="List available event types",
)
async def list_event_types(request: Request) -> dict[str, list[str]]:
    """List all available webhook event types."""
    return {"event_types": ALL_EVENT_TYPES}
