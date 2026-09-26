"""Crew inboxes, read cursors and notifications REST API (spec §5.8, §6 "Inbox" and "Notifications", §9.11).

* ``GET /crews/inbox/overview``: "My inbox", live Needs-you items across every
  crew the caller can see (project-restricted keys: their projects only).
* ``GET /crews/{id}/inbox?audience=project|crew|me``: Needs-you, Crew, or the
  caller's own queue (a crew session's queue; for a human, Needs-you).
* Item actions: **Needs-you items are for humans** (only a dashboard login may
  mark, resolve or dismiss them, so an agent cannot hide a safety alarm);
  crew items may be claimed (first taker wins) and resolved by any member with
  ``crew:write``, dismissed only by a human; session items belong to their
  session (or a human).
* ``POST /crews/{id}/read``: advance a read cursor (never backwards).
* ``GET/PATCH /notifications``, ``GET /notifications/rules`` (L0 defaults) and
  ``POST /notifications/targets`` (human only; webhooks must pass a signed
  challenge).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.crew.access import (
    CrewAccess,
    CrewEntity,
    crew_access,
    crew_entity,
    crew_error,
    get_crew_conn,
    human_principal,
    key_permissions,
    require_human,
)
from remembra.crew.channel import SESSION_HEADER, authenticate_session
from remembra.crew.events import CrewEventLog
from remembra.crew.inbox import Author, CrewInbox, InboxError
from remembra.crew.limits import enforce_rate_limit
from remembra.crew.notify import (
    INAPP_STREAM,
    NotifyTargets,
    WebhookSender,
    default_rules_view,
    list_notifications,
)
from remembra.crew.store import CrewStore

router = APIRouter(tags=["crew-inbox"])


def event_log(request: Request) -> CrewEventLog:
    log: CrewEventLog | None = getattr(request.app.state, "crew_events", None)
    if log is None:
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return log


def crew_db(request: Request) -> Any:
    """The ``crew.db`` handle behind the event log (fetchone/fetchall/transaction)."""
    return event_log(request).db


def as_http(e: InboxError) -> Exception:
    return crew_error(e.status, e.error, e.message)


async def optional_session(request: Request, access: CrewAccess) -> Author | None:
    """The caller's crew session when the session-token header is present (verified), else None."""
    if not request.headers.get(SESSION_HEADER):
        return None
    try:
        return await authenticate_session(
            event_log(request).db.conn,
            access.crew.id,
            access.user.user_id,
            request.headers.get(SESSION_HEADER),
            agent_id=access.user.agent_id,
        )
    except InboxError as e:
        raise as_http(e) from e


def _require_read_key(user: AuthenticatedUser) -> None:
    if "crew:read" not in key_permissions(user):
        raise crew_error(status.HTTP_403_FORBIDDEN, "forbidden", "Insufficient permissions. Required: crew:read")


async def _visible_crews(request: Request, user: AuthenticatedUser) -> list[dict[str, Any]]:
    get_crew_conn(request)  # 503 when crew mode is off
    store = CrewStore(crew_db(request))
    return await store.list_crews_for_user(user.user_id, list(user.project_ids) if user.project_ids else None)


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


@router.get("/crews/inbox/overview", summary="My inbox: Needs-you across projects")
async def inbox_overview(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    _require_read_key(user)
    crews = await _visible_crews(request, user)
    return await CrewInbox(event_log(request)).overview(crews, limit=limit)


@router.get("/crews/{crew_id}/inbox", summary="Inbox items (?audience=project|crew|me)")
async def list_inbox(
    request: Request,
    access: Annotated[CrewAccess, Depends(crew_access("crew:read"))],
    audience: Annotated[Literal["project", "crew", "me"], Query()] = "project",
    include_closed: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    inbox = CrewInbox(event_log(request))
    recipient: str | None = None
    real: str = audience
    if audience == "me":
        session = await optional_session(request, access)
        if session is not None:
            real, recipient = "session", session.session_id
        else:
            real = "project"
    items = await inbox.list_items(access.crew.id, audience=real, recipient=recipient, include_closed=include_closed, limit=limit)
    return {"audience": real, "recipient": recipient, "items": items, "counts": await inbox.counts(access.crew.id)}


async def _item_actor(request: Request, ent: CrewEntity, action: str) -> Author:
    """Who may act on this item (see the module docstring); 403 otherwise."""
    row = ent.row
    access = ent.access
    if access.human:
        return Author.human(access.user.user_id)
    audience = row["audience"]
    if audience == "project":
        require_human(access.user)
    session = await optional_session(request, access)
    if session is None:
        raise crew_error(status.HTTP_401_UNAUTHORIZED, "session_auth", "a crew session id and token are required")
    if audience == "crew" and action == "dismiss":
        require_human(access.user)
    if audience == "session" and row["recipient"] != session.session_id:
        raise crew_error(status.HTTP_403_FORBIDDEN, "forbidden", "This item belongs to another session's queue.")
    return session


def _entity_dep() -> Any:
    return crew_entity("inbox_item", "crew:write")


@router.post("/inbox/items/{item_id}/seen", summary="Mark seen")
async def item_seen(request: Request, ent: Annotated[CrewEntity, Depends(_entity_dep())]) -> dict[str, Any]:
    await _item_actor(request, ent, "seen")
    try:
        return {"item": await CrewInbox(event_log(request)).mark_seen(ent.id), "seq": None}
    except InboxError as e:
        raise as_http(e) from e


@router.post("/inbox/items/{item_id}/claim", summary="Claim (first taker wins)")
async def item_claim(request: Request, ent: Annotated[CrewEntity, Depends(_entity_dep())]) -> dict[str, Any]:
    who = await _item_actor(request, ent, "claim")
    if ent.row["audience"] != "crew":
        raise crew_error(status.HTTP_409_CONFLICT, "inbox_state", "Only crew inbox items can be claimed.")
    try:
        item, seq = await CrewInbox(event_log(request)).claim(ent.id, claimer=who.principal, actor=who.actor())
    except InboxError as e:
        raise as_http(e) from e
    return {"item": item, "seq": seq}


@router.post("/inbox/items/{item_id}/resolve", summary="Resolve")
async def item_resolve(request: Request, ent: Annotated[CrewEntity, Depends(_entity_dep())]) -> dict[str, Any]:
    who = await _item_actor(request, ent, "resolve")
    try:
        item, seq = await CrewInbox(event_log(request)).resolve(ent.id, by=who.principal, actor=who.actor())
    except InboxError as e:
        raise as_http(e) from e
    return {"item": item, "seq": seq}


@router.post("/inbox/items/{item_id}/dismiss", summary="Dismiss")
async def item_dismiss(request: Request, ent: Annotated[CrewEntity, Depends(_entity_dep())]) -> dict[str, Any]:
    who = await _item_actor(request, ent, "dismiss")
    try:
        item, seq = await CrewInbox(event_log(request)).resolve(ent.id, by=who.principal, actor=who.actor(), dismiss=True)
    except InboxError as e:
        raise as_http(e) from e
    return {"item": item, "seq": seq}


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: str = Field(..., min_length=1, max_length=32)
    seq: int = Field(..., ge=0)


@router.post("/crews/{crew_id}/read", summary="Advance a read cursor")
async def advance_read(
    request: Request,
    payload: Annotated[ReadBody, Body(...)],
    access: Annotated[CrewAccess, Depends(crew_access("crew:read"))],
) -> dict[str, Any]:
    session = await optional_session(request, access)
    principal = session.session_id if session is not None else access.user.user_id
    assert principal is not None
    try:
        value = await CrewInbox(event_log(request)).advance_cursor(access.crew.id, principal, payload.stream, payload.seq)
    except InboxError as e:
        raise as_http(e) from e
    return {"stream": payload.stream, "last_seq": value, "principal": principal}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/notifications", summary="List notifications")
async def get_notifications(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    crew_id: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    _require_read_key(user)
    crews = await _visible_crews(request, user)
    return await list_notifications(event_log(request).db, crews, user.user_id, limit=limit, crew_id=crew_id)


class MarkReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crew_id: str | None = Field(default=None, max_length=80)
    upto_seq: int | None = Field(default=None, ge=0)
    all: bool = False

    @model_validator(mode="after")
    def _one_form(self) -> MarkReadBody:
        if self.all == (self.crew_id is not None):
            raise ValueError("pass either {all: true} or {crew_id, upto_seq?}")
        return self


@router.patch("/notifications", summary="Mark notifications read")
async def mark_notifications(
    request: Request,
    payload: Annotated[MarkReadBody, Body(...)],
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_read_key(user)
    crews = await _visible_crews(request, user)
    inbox = CrewInbox(event_log(request))
    targets = crews if payload.all else [c for c in crews if c["id"] == payload.crew_id]
    if not payload.all and not targets:
        raise crew_error(status.HTTP_404_NOT_FOUND, "not_found", "Not found.")
    for crew in targets:
        upto = int(crew["last_seq"]) if payload.upto_seq is None else min(int(payload.upto_seq), int(crew["last_seq"]))
        try:
            await inbox.advance_cursor(str(crew["id"]), user.user_id, INAPP_STREAM, upto)
        except InboxError as e:
            raise as_http(e) from e
    return await list_notifications(event_log(request).db, crews, user.user_id, limit=1)


@router.get("/notifications/rules", summary="Notification rules (L0 defaults)")
async def get_notification_rules(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_read_key(user)
    db = crew_db(request)
    rows = await db.fetchall(
        "SELECT crew_id, kind, channel, quiet_hours, batch_window_s FROM crew_notification_rules WHERE user_id = ?",
        (user.user_id,),
    )
    from remembra.crew.notify import BATCH_WINDOW_S, DEFAULT_QUIET_TZ

    return {
        "defaults": default_rules_view(),
        "rules": rows,
        "targets": await NotifyTargets(db).list(user.user_id),
        "batch_window_s": BATCH_WINDOW_S,
        "quiet_hours_tz": DEFAULT_QUIET_TZ,
    }


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["email", "webhook"]
    target: str = Field(..., min_length=3, max_length=2048)


def webhook_sender(request: Request) -> WebhookSender:
    """The sender used for challenges (``app.state.crew_webhook_sender`` overrides it, e.g. in tests)."""
    sender: WebhookSender | None = getattr(request.app.state, "crew_webhook_sender", None)
    return sender or WebhookSender()


async def _verified_account_email(request: Request, user: AuthenticatedUser) -> str | None:
    """The caller's own login email when it is verified (such a target needs no confirmation code)."""
    db = getattr(request.app.state, "db", None)
    if db is None or not hasattr(db, "get_user_by_id"):
        return None
    row = await db.get_user_by_id(user.user_id)
    if not row or not row.get("email") or not row.get("email_verified"):
        return None
    return str(row["email"])


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=4, max_length=32)


@router.post("/notifications/targets/{target_id}/confirm", summary="Confirm an email target with the mailed code")
async def confirm_notification_target(
    request: Request,
    target_id: str,
    payload: Annotated[ConfirmBody, Body(...)],
    user: Annotated[AuthenticatedUser, Depends(human_principal())],
) -> dict[str, Any]:
    enforce_rate_limit("notify_confirm", user_id=user.user_id)  # also bounds guessing the code
    targets = NotifyTargets(event_log(request).db)
    try:
        return await targets.confirm(user.user_id, target_id, payload.code)
    except InboxError as e:
        raise as_http(e) from e


@router.post("/notifications/targets", status_code=status.HTTP_201_CREATED, summary="Add an email or signed-webhook target")
async def add_notification_target(
    request: Request,
    payload: Annotated[TargetBody, Body(...)],
    user: Annotated[AuthenticatedUser, Depends(human_principal())],
) -> dict[str, Any]:
    if payload.kind == "email":
        # each unconfirmed address gets a mailed code: bounded per user (a confirmation is still a mail)
        enforce_rate_limit("notify_confirm", user_id=user.user_id)
    targets = NotifyTargets(
        event_log(request).db,
        webhooks=webhook_sender(request),
        email_backend=getattr(request.app.state, "crew_email_backend", None),
        account_email=await _verified_account_email(request, user),
    )
    try:
        out = await targets.add(user.user_id, payload.kind, payload.target)
    except InboxError as e:
        raise as_http(e) from e
    crews = await _visible_crews(request, user)
    from remembra.crew.settings import load_settings

    out["crews_without_channel"] = [
        c["id"]
        for c in crews
        if payload.kind not in ((load_settings(c["settings"]).get("notify") or {}).get("realtime") or [])
        and c.get("role") in ("owner", "admin")
    ]
    return out
