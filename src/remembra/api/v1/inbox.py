"""
Agent inbox endpoints — /api/v1/inbox.

Targeted agent-to-agent message delivery for single-tenant deployments.
Each row is scoped to the authenticated user (owner_user_id). The
`to_agent` / `from_agent` fields are free-form logical names chosen by
the caller (e.g. "trademind-trading", "charthustle-holding").

Implements GitHub issue #9 (Agent inbox pattern for targeted pickup).
"""

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.core.limiter import limiter
from remembra.inbox.manager import TERMINAL_STATUSES, InboxManager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_inbox_manager(request: Request) -> InboxManager:
    manager: InboxManager | None = getattr(request.app.state, "inbox_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent inbox is not available on this server.",
        )
    return manager


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SendInboxRequest(BaseModel):
    to_agent: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Logical recipient agent id (free-form).",
    )
    subject: str = Field(..., min_length=1, max_length=256)
    body: str = Field(..., min_length=1, max_length=50000)
    from_agent: str | None = Field(
        default=None,
        max_length=128,
        description="Optional sender id. If omitted, defaults to 'unknown'.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = Field(
        default=None,
        description="Optional expiry. Rows past this are filtered from get_inbox.",
    )

    @field_validator("to_agent", "subject", "from_agent")
    @classmethod
    def strip_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip()


class InboxRow(BaseModel):
    inbox_id: str
    from_agent: str
    to_agent: str
    subject: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: str
    ack_at: str | None = None
    ack_note: str | None = None
    ack_result: str | None = None
    expires_at: str | None = None


class SendInboxResponse(BaseModel):
    inbox_id: str
    status: str
    created_at: str


class AckInboxRequest(BaseModel):
    result: Literal["done", "blocked", "rejected"] | None = Field(
        default=None,
        description=(f"Terminal ack status. Omit to simply mark the row as 'read'. Allowed: {sorted(TERMINAL_STATUSES)}"),
    )
    note: str | None = Field(default=None, max_length=4000)


class AckInboxResponse(BaseModel):
    inbox_id: str
    status: str
    ack_at: str
    ack_result: str | None = None
    ack_note: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/send",
    response_model=SendInboxResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a targeted message to an agent inbox",
)
@limiter.limit("120/minute")
async def send_to_inbox(
    request: Request,
    payload: Annotated[SendInboxRequest, Body(...)],
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
) -> SendInboxResponse:
    """Write an inbox row addressed to `payload.to_agent`."""
    try:
        row = await inbox.send(
            owner_user_id=current_user.user_id,
            from_agent=payload.from_agent or "unknown",
            to_agent=payload.to_agent,
            subject=payload.subject,
            body=payload.body,
            metadata=payload.metadata,
            expires_at=payload.expires_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception("inbox_send_failed user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send inbox message. Please try again later.",
        ) from e

    return SendInboxResponse(
        inbox_id=row["inbox_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


@router.get(
    "",
    response_model=list[InboxRow],
    summary="List inbox items addressed to a given agent_id",
)
@limiter.limit("240/minute")
async def get_inbox(
    request: Request,
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
    agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    # Aliased to keep the "status" query-param name while avoiding shadowing the
    # `fastapi.status` module — the error handlers below reference status.HTTP_*.
    status_filter: Annotated[
        Literal["unread", "all"],
        Query(alias="status", description="'unread' (default) or 'all'."),
    ] = "unread",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[InboxRow]:
    """Return inbox rows for `agent_id` (scoped to the authenticated user)."""
    try:
        rows = await inbox.get_for_agent(
            owner_user_id=current_user.user_id,
            agent_id=agent_id,
            status=status_filter,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception("inbox_get_failed user=%s agent=%s", current_user.user_id, agent_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read inbox. Please try again later.",
        ) from e

    return [InboxRow(**row) for row in rows]


@router.post(
    "/{inbox_id}/ack",
    response_model=AckInboxResponse,
    summary="Acknowledge an inbox item",
)
@limiter.limit("240/minute")
async def ack_inbox(
    request: Request,
    inbox_id: str,
    payload: Annotated[AckInboxRequest, Body(...)],
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
) -> AckInboxResponse:
    """Mark an inbox row read/done/blocked/rejected with an optional note."""
    try:
        row = await inbox.ack(
            owner_user_id=current_user.user_id,
            inbox_id=inbox_id,
            result=payload.result,
            note=payload.note,
        )
    except ValueError as e:
        # Missing row → 404; bad result value → 400
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg) from e
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from e
    except Exception as e:
        log.exception("inbox_ack_failed user=%s id=%s", current_user.user_id, inbox_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ack inbox item. Please try again later.",
        ) from e

    return AckInboxResponse(
        inbox_id=row["inbox_id"],
        status=row["status"],
        ack_at=row["ack_at"] or "",
        ack_result=row.get("ack_result"),
        ack_note=row.get("ack_note"),
    )
