"""Crew channel and decisions REST API (spec §5.7, §6 "Channel" and "Decisions").

Every ``/crews/{crew_id}/…`` route resolves the crew through ``crew_access``
(``load_crew``) and every entity route through ``crew_entity``
(``load_crew_entity``, 404 on any ACL failure). Human-only routes (redact, pin,
confirm, reject, supersede) use the human-only permissions, so an API key gets
403 whatever its role (D27).

An agent writes through its **crew session**: the request carries
``X-Remembra-Crew-Session`` carrying the session token ``join`` returned (the one
session-token header every crew route uses). The author, callsign and key-verified flag come from
that session, never from the body. A dashboard login writes as the human.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from remembra.crew import schemas
from remembra.crew.access import CrewAccess, CrewEntity, crew_access, crew_entity, crew_error
from remembra.crew.channel import SESSION_HEADER, CrewChannel, authenticate_session
from remembra.crew.decisions import CrewDecisions, CrewRef
from remembra.crew.events import CrewEventLog, IdempotencyConflict, idem_lookup, idem_store
from remembra.crew.inbox import Author, CrewInbox, InboxError
from remembra.crew.limits import enforce_rate_limit

router = APIRouter(tags=["crew-channel"])


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def event_log(request: Request) -> CrewEventLog:
    log: CrewEventLog | None = getattr(request.app.state, "crew_events", None)
    if log is None:
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return log


def _outbox_wake(request: Request) -> Any:
    worker = getattr(request.app.state, "crew_outbox", None)
    return worker.wake if worker is not None else None


def services(request: Request) -> tuple[CrewChannel, CrewDecisions, CrewInbox]:
    log = event_log(request)
    inbox = CrewInbox(log)
    decisions = CrewDecisions(log, inbox)
    channel = CrewChannel(
        log, inbox=inbox, decisions=decisions, bus=getattr(request.app.state, "crew_bus", None), outbox_wake=_outbox_wake(request)
    )
    return channel, decisions, inbox


def as_http(e: InboxError) -> Exception:
    return crew_error(e.status, e.error, e.message)


def crew_ref(access: CrewAccess) -> CrewRef:
    return CrewRef(id=access.crew.id, owner_user_id=access.crew.owner_user_id, project_id=access.crew.project_id)


async def author_for(request: Request, access: CrewAccess) -> Author:
    """The writer: the human for a dashboard login (the (H) principal only as crew owner or admin), else the crew
    session proven by its headers (401 otherwise)."""
    if access.human:
        return Author.human(access.user.user_id, privileged=access.privileged)
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


def _limit_messages(request: Request, access: CrewAccess) -> None:
    enforce_rate_limit("messages", user_id=access.user.user_id, session_token=request.headers.get(SESSION_HEADER))


# ---------------------------------------------------------------------------
# Bodies (closed objects)
# ---------------------------------------------------------------------------


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., max_length=32)
    body: str = Field(..., min_length=1, max_length=schemas.MAX_MESSAGE_BYTES)
    thread_root_id: str | None = Field(default=None, max_length=80)
    reply_to_id: str | None = Field(default=None, max_length=80)
    refs: list[str] | None = Field(default=None, max_length=20)
    client_msg_id: str = Field(..., min_length=1, max_length=64)
    wait_s: int = Field(default=0, ge=0, le=schemas.SAY_WAIT_MAX_S)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(..., min_length=1, max_length=schemas.MAX_MESSAGE_BYTES)


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool = True


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    decision: str = Field(..., min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=2000)
    alternatives: list[str] | None = Field(default=None, max_length=10)
    task_id: str | None = Field(default=None, max_length=80)
    zone_id: str | None = Field(default=None, max_length=80)
    # Rider (gap analysis §7): what the decision rests on (commit shas, paths, test names, links).
    evidence: list[str] | None = Field(default=None, max_length=10)


class SupersedeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    decision: str = Field(..., min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/messages", summary="List messages (?thread=&since_seq=&before=)")
async def list_messages(
    request: Request,
    access: Annotated[CrewAccess, Depends(crew_access("crew:read"))],
    thread: Annotated[str | None, Query(max_length=80)] = None,
    since_seq: Annotated[int | None, Query(ge=0)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    channel, _, _ = services(request)
    items = await channel.list_messages(access.crew.id, thread=thread, since_seq=since_seq, before=before, limit=limit)
    return {"items": items, "last_seq": items[-1]["seq"] if items else None}


@router.post(
    "/crews/{crew_id}/messages",
    status_code=status.HTTP_201_CREATED,
    summary="Post a message (long-poll wait_s ≤120 for the first reply in the thread)",
)
async def post_message(
    request: Request,
    payload: Annotated[MessageBody, Body(...)],
    access: Annotated[CrewAccess, Depends(crew_access("crew:write"))],
) -> dict[str, Any]:
    author = await author_for(request, access)
    _limit_messages(request, access)
    channel, _, _ = services(request)
    try:
        result = await channel.post(
            crew_ref(access),
            author,
            kind=payload.kind,
            body=payload.body,
            client_msg_id=payload.client_msg_id,
            thread_root_id=payload.thread_root_id,
            reply_to_id=payload.reply_to_id,
            refs=payload.refs,
            wait_s=payload.wait_s,
        )
    except InboxError as e:
        raise as_http(e) from e
    return result.to_dict()


@router.patch("/messages/{message_id}", summary="Edit own message (≤10 min)")
async def edit_message(
    request: Request,
    payload: Annotated[EditBody, Body(...)],
    ent: Annotated[CrewEntity, Depends(crew_entity("message", "crew:write"))],
) -> dict[str, Any]:
    author = await author_for(request, ent.access)
    _limit_messages(request, ent.access)
    channel, _, _ = services(request)
    try:
        return await channel.edit(ent.id, author, body=payload.body)
    except InboxError as e:
        raise as_http(e) from e


@router.post("/messages/{message_id}/redact", summary="Redact a message")
async def redact_message(
    request: Request,
    ent: Annotated[CrewEntity, Depends(crew_entity("message", "crew:admin"))],
) -> dict[str, Any]:
    channel, _, _ = services(request)
    try:
        return await channel.redact(ent.id, Author.human(ent.access.user.user_id, privileged=ent.access.privileged))
    except InboxError as e:
        raise as_http(e) from e


@router.post("/messages/{message_id}/pin", summary="Pin a message")
async def pin_message(
    request: Request,
    ent: Annotated[CrewEntity, Depends(crew_entity("message", "crew:admin"))],
    payload: Annotated[PinBody | None, Body()] = None,
) -> dict[str, Any]:
    channel, _, _ = services(request)
    try:
        return await channel.pin(
            ent.id,
            Author.human(ent.access.user.user_id, privileged=ent.access.privileged),
            pinned=payload.pinned if payload else True,
        )
    except InboxError as e:
        raise as_http(e) from e


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/decisions", summary="List decisions")
async def list_decisions(
    request: Request,
    access: Annotated[CrewAccess, Depends(crew_access("crew:read"))],
    state: Annotated[list[Literal["proposed", "in_force", "rejected", "superseded"]] | None, Query()] = None,
) -> dict[str, Any]:
    _, decisions, _ = services(request)
    return {"items": await decisions.list_decisions(access.crew.id, states=state)}


@router.post("/crews/{crew_id}/decisions", status_code=status.HTTP_201_CREATED, summary="Create a decision (agent → proposed)")
async def create_decision(
    request: Request,
    payload: Annotated[DecisionBody, Body(...)],
    access: Annotated[CrewAccess, Depends(crew_access("crew:write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> dict[str, Any]:
    author = await author_for(request, access)
    _, decisions, _ = services(request)
    route = f"POST /crews/{access.crew.id}/decisions"
    body = payload.model_dump()
    conn = decisions.db.conn
    if idempotency_key:
        try:
            cached = await idem_lookup(conn, principal=author.principal, route=route, key=idempotency_key, body=body)
        except IdempotencyConflict as e:
            raise crew_error(422, "idempotency_conflict", str(e)) from e
        if cached is not None:
            return cached
    try:
        out = await decisions.create(crew_ref(access), author, **body)
    except InboxError as e:
        raise as_http(e) from e
    if idempotency_key:
        async with decisions.db.transaction():
            await idem_store(conn, principal=author.principal, route=route, key=idempotency_key, body=body, response=out)
    return out


@router.post("/decisions/{decision_id}/confirm", summary="Confirm a decision")
async def confirm_decision(
    request: Request,
    ent: Annotated[CrewEntity, Depends(crew_entity("decision", "crew:override"))],
) -> dict[str, Any]:
    _, decisions, _ = services(request)
    try:
        return await decisions.confirm(
            ent.id, Author.human(ent.access.user.user_id, privileged=ent.access.privileged), crew_ref(ent.access)
        )
    except InboxError as e:
        raise as_http(e) from e


@router.post("/decisions/{decision_id}/reject", summary="Reject a decision")
async def reject_decision(
    request: Request,
    ent: Annotated[CrewEntity, Depends(crew_entity("decision", "crew:override"))],
) -> dict[str, Any]:
    _, decisions, _ = services(request)
    try:
        return await decisions.reject(
            ent.id, Author.human(ent.access.user.user_id, privileged=ent.access.privileged), crew_ref(ent.access)
        )
    except InboxError as e:
        raise as_http(e) from e


@router.post("/decisions/{decision_id}/supersede", summary="Supersede a decision")
async def supersede_decision(
    request: Request,
    ent: Annotated[CrewEntity, Depends(crew_entity("decision", "crew:override"))],
    payload: Annotated[SupersedeBody | None, Body()] = None,
) -> dict[str, Any]:
    if payload is None:
        raise crew_error(422, "validation_error", "title and decision of the replacement are required")
    _, decisions, _ = services(request)
    try:
        return await decisions.supersede(
            ent.id,
            Author.human(ent.access.user.user_id, privileged=ent.access.privileged),
            crew_ref(ent.access),
            title=payload.title,
            decision=payload.decision,
            rationale=payload.rationale,
        )
    except InboxError as e:
        raise as_http(e) from e
