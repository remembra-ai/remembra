"""
Agent inbox endpoints — /api/v1/inbox.

Targeted agent-to-agent message delivery for single-tenant deployments.
Each row is scoped to the authenticated user (owner_user_id). The
`to_agent` / `from_agent` fields are free-form logical names chosen by
the caller (e.g. "trademind-trading", "charthustle-holding").

Implements GitHub issue #9 (Agent inbox pattern for targeted pickup).

Scoping (every route):

* **Project scoping.** Every route honours the key's project allow-list
  (``AuthenticatedUser.project_ids``): a restricted key sees and acks only rows
  tagged with one of its projects; rows with no project are invisible to it.
  Reads take ``?project_id=`` (one project) and ``?scope=all|project|unscoped``.
  A restricted key must send into one of its projects (the only one when it
  has exactly one).
* **Agent scoping.** An agent-scoped key (or an agent-bound connector grant)
  reads and acks only its own inbox: rows addressed to its agent. Asking for
  another agent's inbox, or acking a row addressed to another agent, is a 404,
  so ids cannot be probed.

Unrestricted keys and dashboard logins keep the full owner view.
"""

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from remembra.auth.middleware import AuthenticatedUser, get_current_user, require_memory_recall, require_memory_store
from remembra.cloud.limits import record_relay_usage, relay_guard
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
ScopeQuery = Annotated[
    Literal["all", "project", "unscoped"],
    Query(description="all (default), project (only rows tagged with a project) or unscoped (rows with none)"),
]
ProjectQuery = Annotated[str | None, Query(max_length=128, description="Only rows of this project")]


def _allowed_projects(user: AuthenticatedUser) -> list[str] | None:
    """The caller's project allow-list (None = unrestricted)."""
    return list(user.project_ids) if user.project_ids else None


def _send_project(user: AuthenticatedUser, requested: str | None) -> str | None:
    """The project a new row is tagged with; a restricted key must stay inside its allow-list."""
    allowed = _allowed_projects(user)
    project = (requested or "").strip() or None
    if allowed is None:
        return project
    if project is None:
        if len(allowed) == 1:
            return allowed[0]
        raise HTTPException(
            status_code=422,
            detail={"error": "project_required", "message": "This key is limited to several projects; pass project_id."},
        )
    if project not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "project_forbidden", "message": "This key cannot send into that project."},
        )
    return project


def _requested_project(payload: "SendInboxRequest") -> str | None:
    """``project_id`` from the body, else the ``metadata.project_id`` tag older clients send."""
    if payload.project_id and payload.project_id.strip():
        return payload.project_id
    tagged = payload.metadata.get("project_id")
    return tagged if isinstance(tagged, str) else None


def _own_agent(user: AuthenticatedUser) -> str | None:
    """The agent an agent-scoped credential is bound to (None = may read every agent's inbox)."""
    agent = (getattr(user, "agent_id", None) or "").strip()
    return agent or None


def _inbox_not_found() -> HTTPException:
    # Same answer for "another agent's inbox" and "no such row": nothing to probe.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")


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
    project_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Project the message belongs to (required for keys limited to several projects). Defaults to metadata.project_id."
        ),
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


def _inbox_row(row: dict[str, Any]) -> InboxRow:
    return InboxRow(**{k: v for k, v in row.items() if k in InboxRow.model_fields})


class SendInboxResponse(BaseModel):
    inbox_id: str
    status: str
    created_at: str
    project_id: str | None = None


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
    dependencies=[require_memory_store()],
)
@limiter.limit("120/minute")
async def send_to_inbox(
    request: Request,
    payload: Annotated[SendInboxRequest, Body(...)],
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
    response: Response,
) -> SendInboxResponse:
    """Write an inbox row addressed to `payload.to_agent`.

    An inbox message is a relay event: free on every plan (never uses smart
    credits), subject only to the plan's relay burst limit.
    """
    project_id = _send_project(current_user, _requested_project(payload))
    metadata = {k: v for k, v in payload.metadata.items() if k != "project_id"}
    await relay_guard(request, response, current_user.user_id)
    try:
        row = await inbox.send(
            owner_user_id=current_user.user_id,
            # An agent-scoped key sends as its own agent, whatever the payload claims.
            from_agent=getattr(current_user, "agent_id", None) or payload.from_agent or "unknown",
            to_agent=payload.to_agent,
            subject=payload.subject,
            body=payload.body,
            metadata=metadata,
            expires_at=payload.expires_at,
            project_id=project_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception("inbox_send_failed user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send inbox message. Please try again later.",
        ) from e

    await record_relay_usage(request, current_user.user_id)
    return SendInboxResponse(
        inbox_id=row["inbox_id"],
        status=row["status"],
        created_at=row["created_at"],
        project_id=row.get("project_id"),
    )


@router.get(
    "",
    response_model=list[InboxRow],
    summary="List inbox items addressed to a given agent_id",
    dependencies=[require_memory_recall()],
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
    project_id: ProjectQuery = None,
    scope: ScopeQuery = "all",
) -> list[InboxRow]:
    """Return inbox rows for `agent_id` (scoped to the authenticated user, the
    key's projects and, for an agent-scoped key, its own agent)."""
    own = _own_agent(current_user)
    if own is not None and agent_id.strip() != own:
        raise _inbox_not_found()
    try:
        rows = await inbox.get_for_agent(
            owner_user_id=current_user.user_id,
            agent_id=agent_id,
            status=status_filter,
            limit=limit,
            project_ids=_allowed_projects(current_user),
            project_id=project_id,
            scope=scope,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception("inbox_get_failed user=%s agent=%s", current_user.user_id, agent_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read inbox. Please try again later.",
        ) from e

    return [_inbox_row(row) for row in rows]


class InboxListResponse(BaseModel):
    items: list[InboxRow]
    total: int
    status: str
    agent_id: str | None = None


class InboxAgentCounts(BaseModel):
    agent_id: str
    unread: int
    open: int
    received: int
    sent: int
    last_at: str | None = None


class InboxSummaryResponse(BaseModel):
    unread_total: int
    open_total: int
    agents: list[InboxAgentCounts]


@router.get(
    "/messages",
    response_model=InboxListResponse,
    summary="List inbox messages across all agents (dashboard view)",
    dependencies=[require_memory_recall()],
)
@limiter.limit("240/minute")
async def list_inbox_messages(
    request: Request,
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
    status_filter: Annotated[
        Literal["unread", "open", "all"],
        Query(alias="status", description="'open' (default: unread or read), 'unread' or 'all'."),
    ] = "open",
    agent_id: Annotated[str | None, Query(max_length=128, description="Only messages to or from this agent")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    project_id: ProjectQuery = None,
    scope: ScopeQuery = "all",
) -> InboxListResponse:
    """Every message of the authenticated owner in the key's projects, newest
    first. An agent-scoped key sees only the messages addressed to its agent.
    Read-only: listing never marks anything read."""
    own = _own_agent(current_user)
    if own is not None and (agent_id or "").strip() not in ("", own):
        raise _inbox_not_found()
    try:
        result = await inbox.list_messages(
            owner_user_id=current_user.user_id,
            status=status_filter,
            agent_id=agent_id,
            limit=limit,
            offset=offset,
            project_ids=_allowed_projects(current_user),
            project_id=project_id,
            scope=scope,
            recipient=own,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception("inbox_list_failed user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read inbox. Please try again later.",
        ) from e
    return InboxListResponse(
        items=[_inbox_row(row) for row in result["items"]],
        total=result["total"],
        status=status_filter,
        agent_id=(agent_id or "").strip() or None,
    )


@router.get(
    "/summary",
    response_model=InboxSummaryResponse,
    summary="Unread / open message counts per agent",
    dependencies=[require_memory_recall()],
)
@limiter.limit("240/minute")
async def inbox_summary(
    request: Request,
    current_user: CurrentUserDep,
    inbox: Annotated[InboxManager, Depends(get_inbox_manager)],
    project_id: ProjectQuery = None,
    scope: ScopeQuery = "all",
) -> InboxSummaryResponse:
    """Counts per agent id (as recipient and as sender) for the authenticated owner
    and the key's projects; an agent-scoped key counts only its own inbox."""
    try:
        result = await inbox.summary(
            current_user.user_id,
            project_ids=_allowed_projects(current_user),
            project_id=project_id,
            scope=scope,
            recipient=_own_agent(current_user),
        )
    except Exception as e:
        log.exception("inbox_summary_failed user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read inbox. Please try again later.",
        ) from e
    return InboxSummaryResponse(**result)


@router.post(
    "/{inbox_id}/ack",
    response_model=AckInboxResponse,
    summary="Acknowledge an inbox item",
    dependencies=[require_memory_store()],
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
            project_ids=_allowed_projects(current_user),
            recipient=_own_agent(current_user),
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
