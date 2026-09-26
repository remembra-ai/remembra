"""Crew sessions and hosts REST routes (WP-4, spec §6 "Hosts" and "Sessions").

Credentials. Every route first authenticates the caller's API key or dashboard
JWT (``get_current_user``). On top of that:

* ``POST /crew/heartbeat`` and ``POST /crew/hosts/{id}/rotate`` need the host
  token (``X-Remembra-Host-Token``);
* ``leave`` and ``stall`` need the session token of the named session
  (``X-Remembra-Crew-Session``);
* ``join`` binds a session to a host only with that host's token, and rotates a
  session token only for the host that owns the session (§4.3);
* pause, resume, request-checkpoint and release-all are **human-only** (D27):
  a dashboard login with crew role owner or admin (``crew_entity(...,
  "crew:override")``); an API key gets 403.

Every refusal uses the ``CrewError`` body (``{error, message, …}``); a session
or crew the caller cannot see is the generic 404. Token-bearing answers (join,
host register/rotate) are never stored in the idempotency table (§4.3).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse

from remembra.api.v1.relay import AGENT_HEADER, effective_agent
from remembra.auth.middleware import AuthenticatedUser, get_current_user, resolve_project_access
from remembra.crew import schemas
from remembra.crew.access import (
    CrewAccess,
    CrewEntity,
    crew_access,
    crew_entity,
    crew_error,
    key_permissions,
    not_found,
    project_visible,
)
from remembra.crew.db import CrewDatabase
from remembra.crew.events import IdempotencyConflict, idem_lookup, idem_store
from remembra.crew.hosts import (
    HOST_TOKEN_HEADER,
    HostError,
    authenticate_host,
    authenticate_host_token,
    host_view,
    register_host,
    rotate_host_token,
    tokens_match,
)
from remembra.crew.limits import enforce_rate_limit
from remembra.crew.reaper import build_sessions_service
from remembra.crew.sessions import SESSION_TOKEN_HEADER, CrewSessions, JoinRequest, SessionError, get_session
from remembra.crew.store import now_iso

router = APIRouter(tags=["crew-sessions"])

IDEMPOTENCY_HEADER = "Idempotency-Key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crew_db(request: Request) -> CrewDatabase:
    db = getattr(request.app.state, "crew_db", None)
    if not isinstance(db, CrewDatabase):
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return db


def _service(request: Request) -> CrewSessions:
    _crew_db(request)
    return build_sessions_service(request.app)


def _validate(body: Any, shape: str) -> dict[str, Any]:
    errors = schemas.validate(body, schemas.REQUEST_SHAPES[shape])
    if errors:
        raise crew_error(422, "validation", "; ".join(errors[:5]), errors=errors[:20])
    assert isinstance(body, dict)
    return body


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise crew_error(422, "validation", "The request body must be JSON.")


def _require_perm(user: AuthenticatedUser, perm: str) -> None:
    if perm not in key_permissions(user):
        raise crew_error(status.HTTP_403_FORBIDDEN, "forbidden", f"Insufficient permissions. Required: {perm}")


def _session_error(e: SessionError) -> Any:
    return crew_error(e.status, e.code, e.message, **e.extra)


def _host_error(e: HostError) -> Any:
    return crew_error(e.status, e.code, e.message)


async def _session_for_token(request: Request, user: AuthenticatedUser, session_id: str, token: str | None) -> dict[str, Any]:
    """The session named in the path, for its own user and session token (§6 ``session`` access)."""
    row = await get_session(_crew_db(request).conn, session_id)
    if row is None or row["user_id"] != user.user_id:
        raise not_found()
    crew = await _crew_db(request).fetchone("SELECT project_id FROM crews WHERE id = ?", (row["crew_id"],))
    if crew is None or not project_visible(user, crew["project_id"]):
        raise not_found()
    # an agent-scoped key acts only as sessions of its own agent (§11.2), whatever token it carries
    if not tokens_match(token, row["token_hash"]) or (user.agent_id is not None and row["agent_id"] != user.agent_id):
        raise crew_error(
            status.HTTP_401_UNAUTHORIZED, "session_token_invalid", f"Send this session's token in {SESSION_TOKEN_HEADER}."
        )
    return row


async def _idempotent(request: Request, principal: str, route: str, key: str | None, body: Any) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        return await idem_lookup(_crew_db(request).conn, principal=principal, route=route, key=key, body=body)
    except IdempotencyConflict as e:
        raise crew_error(422, "idempotency_conflict", str(e))


async def _remember(request: Request, principal: str, route: str, key: str | None, body: Any, response: dict[str, Any]) -> None:
    if not key:
        return
    db = _crew_db(request)
    async with db.transaction():
        await idem_store(db.conn, principal=principal, route=route, key=key, body=body, response=response)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


@router.post("/crew/hosts/register", status_code=status.HTTP_201_CREATED)
async def register_crew_host(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, Any]:
    """Register this machine's crewd; returns the host token once (only its hash is stored)."""
    _require_perm(user, "crew:write")
    enforce_rate_limit("join", user_id=user.user_id)
    body = _validate(await _json(request), "HostRegister")
    db = _crew_db(request)
    try:
        host = await register_host(
            db,
            user_id=user.user_id,
            host_label=body["host_label"],
            platform=body["platform"],
            crewd_version=body["crewd_version"],
            now=now_iso(),
        )
    except HostError as e:
        raise _host_error(e)
    return {"host_id": host.row["id"], "host_token": host.token, "host": host_view(host.row)}


@router.post("/crew/hosts/{host_id}/rotate")
async def rotate_crew_host(
    host_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    host_token: str | None = Header(default=None, alias=HOST_TOKEN_HEADER),
) -> dict[str, Any]:
    """Replace the host token; the current one must be presented and stops working at once."""
    _require_perm(user, "crew:write")
    enforce_rate_limit("join", user_id=user.user_id)
    try:
        host = await rotate_host_token(_crew_db(request), user_id=user.user_id, host_id=host_id, token=host_token)
    except HostError as e:
        raise _host_error(e)
    return {"host_id": host.row["id"], "host_token": host.token, "host": host_view(host.row)}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def _resolve_join_project(request: Request, user: AuthenticatedUser, body: dict[str, Any]) -> str:
    """``project_id`` or ``locator`` → the project, read-only; restricted keys see only their projects (404)."""
    project_id = (body.get("project_id") or "").strip() or None
    locator = (body.get("locator") or "").strip() or None
    if not project_id and not locator:
        raise crew_error(422, "validation", "Provide project_id or locator.")
    if locator and not project_id:
        from remembra.relay.identity import ProjectLocator
        from remembra.services.relay import ProjectAccessDenied, ProjectRegistry

        main_db = getattr(request.app.state, "db", None)
        if main_db is None:
            raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Project resolution is not available.")
        loc = ProjectLocator(root_path=locator) if locator.startswith(("/", "~")) else ProjectLocator(git_remote=locator)
        try:
            resolved = await ProjectRegistry(main_db).resolve(
                user.user_id, loc, create=False, allowed_projects=list(user.project_ids or []) or None
            )
        except ProjectAccessDenied:
            raise not_found()
        except ValueError as e:
            raise crew_error(422, "validation", str(e))
        project_id = str(resolved["project_id"])
    assert project_id is not None
    if not project_visible(user, project_id):
        raise not_found()
    return resolve_project_access(user, project_id) or project_id


@router.post("/crews/join")
async def join_crew(
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
    host_token: str | None = Header(default=None, alias=HOST_TOKEN_HEADER),
    session_token: str | None = Header(default=None, alias=SESSION_TOKEN_HEADER),
) -> dict[str, Any]:
    """Join (or re-join) the caller's crew for a project, creating the crew on first join (§2, §4.3).

    New session: 201 with the session token (returned once). Re-join with the
    current token: 200, no token. Re-join by crewd with the owning host's token:
    200 with a rotated token. Anything else: 409 ``session_exists``.
    """
    _require_perm(user, "crew:write")
    enforce_rate_limit("join", user_id=user.user_id)
    body = _validate(await _json(request), "Join")
    agent_id, verified = effective_agent(request, user, body["agent_id"])
    if not agent_id or agent_id != body["agent_id"]:
        raise crew_error(422, "validation", f"agent_id must match the key's agent (or the {AGENT_HEADER} header).")
    project_id = await _resolve_join_project(request, user, body)
    req = JoinRequest(
        agent_id=agent_id,
        agent_verified=verified,
        project_id=project_id,
        session_id=body["session_id"],
        adapter=body["adapter"],
        client_kind=body["client_kind"],
        source=body["source"],
        host_id=body.get("host_id"),
        checkout_fp=body.get("checkout_fp"),
        worktree_id=body.get("worktree_id"),
        branch=body.get("branch"),
        head=body.get("head"),
        zones_sha=body.get("zones_sha"),
        model=body.get("model"),
        resume_of=body.get("resume_of"),
        provider=body.get("provider"),
        parent_session_id=body.get("parent_session_id"),
        sub_agent_id=body.get("sub_agent_id"),
        capabilities=body.get("capabilities"),
        run_id=body.get("run_id"),
        context_window=body.get("context_window"),
    )
    try:
        result = await _service(request).join(user_id=user.user_id, req=req, host_token=host_token, session_token=session_token)
    except SessionError as e:
        raise _session_error(e)
    response.status_code = status.HTTP_200_OK if result.rejoined else status.HTTP_201_CREATED
    return result.to_response()


@router.post("/crew/heartbeat")
async def crew_heartbeat(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    host_token: str | None = Header(default=None, alias=HOST_TOKEN_HEADER),
) -> dict[str, Any]:
    """Batched host heartbeat (every 60 s). Renews leases; never an event (§4.2). Natural key ``(host, batch_id)``."""
    _require_perm(user, "crew:write")
    db = _crew_db(request)
    try:
        host = await authenticate_host_token(db.conn, user_id=user.user_id, token=host_token)
    except HostError as e:
        raise _host_error(e)
    enforce_rate_limit("heartbeat", user_id=user.user_id, host_token=host["token_hash"])
    body = _validate(await _json(request), "Heartbeat")
    principal = f"host:{host['id']}"
    replay = await _idempotent(request, principal, "heartbeat", body["batch_id"], body)
    if replay is not None:
        return {**replay, "replayed": True}
    result = await _service(request).heartbeat(user_id=user.user_id, host=host, body=body)
    await _remember(request, principal, "heartbeat", body["batch_id"], body, result)
    return result


@router.post("/sessions/{session_id}/leave")
async def leave_session(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    session_token: str | None = Header(default=None, alias=SESSION_TOKEN_HEADER),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
) -> dict[str, Any]:
    """SessionEnd / orphan (``reason: process_exited``). Reserves or releases claims, stalls unfinished tasks."""
    _require_perm(user, "crew:write")
    enforce_rate_limit("join", user_id=user.user_id)
    row = await _session_for_token(request, user, session_id, session_token)
    body = _validate(await _json(request), "Leave")
    principal = f"session:{row['id']}"
    replay = await _idempotent(request, principal, "leave", idempotency_key, body)
    if replay is not None:
        return replay
    try:
        result = await _service(request).leave(
            row,
            reason=body["reason"],
            facts=body["facts"],
            summary=body.get("summary"),
            baton=bool(body.get("baton")),
            baton_ref=body.get("baton_ref"),
        )
    except SessionError as e:
        raise _session_error(e)
    await _remember(request, principal, "leave", idempotency_key, body, result)
    return result


@router.post("/sessions/{session_id}/stall")
async def stall_session(
    session_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    session_token: str | None = Header(default=None, alias=SESSION_TOKEN_HEADER),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
) -> dict[str, Any]:
    """StopFailure (§8.2). Quota errors block the session and hand the baton on; others checkpoint only."""
    _require_perm(user, "crew:write")
    row = await _session_for_token(request, user, session_id, session_token)
    body = _validate(await _json(request), "Stall")
    principal = f"session:{row['id']}"
    replay = await _idempotent(request, principal, "stall", idempotency_key, body)
    if replay is not None:
        return replay
    try:
        result = await _service(request).stall(
            row,
            error=body["error"],
            facts=body["facts"],
            baton_ref=body.get("baton_ref"),
            last_assistant_message=body.get("last_assistant_message"),
        )
    except SessionError as e:
        raise _session_error(e)
    await _remember(request, principal, "stall", idempotency_key, body, result)
    return result


async def _human_session_action(request: Request, ent: CrewEntity, action: str) -> dict[str, Any]:
    body = _validate(await _json(request), "Reason")
    service = _service(request)
    handler = {
        "pause": service.pause,
        "resume": service.resume,
        "request_checkpoint": service.request_checkpoint,
        "release_all": service.release_all,
    }[action]
    try:
        return await handler(ent.row, human_user_id=ent.access.user.user_id, reason=body["reason"])
    except SessionError as e:
        raise _session_error(e)


@router.post("/sessions/{session_id}/pause")
async def pause_session(request: Request, ent: CrewEntity = Depends(crew_entity("session", "crew:override"))) -> dict[str, Any]:
    """(H) Pause a session: its gate denies every write (§5.2 row 1) until a human resumes it."""
    return await _human_session_action(request, ent, "pause")


@router.post("/sessions/{session_id}/resume")
async def resume_session(request: Request, ent: CrewEntity = Depends(crew_entity("session", "crew:override"))) -> dict[str, Any]:
    """(H) Resume a paused session."""
    return await _human_session_action(request, ent, "resume")


@router.post("/sessions/{session_id}/request-checkpoint")
async def request_session_checkpoint(
    request: Request, ent: CrewEntity = Depends(crew_entity("session", "crew:override"))
) -> dict[str, Any]:
    """(H) Ask the session for a checkpoint (delivered on its next heartbeat)."""
    return await _human_session_action(request, ent, "request_checkpoint")


@router.post("/sessions/{session_id}/release-all")
async def release_all_session_claims(
    request: Request, ent: CrewEntity = Depends(crew_entity("session", "crew:override"))
) -> dict[str, Any]:
    """(H) Release every live claim of the session (§5.1), reserved batons included."""
    return await _human_session_action(request, ent, "release_all")


@router.get("/crews/{crew_id}/sessions")
async def list_crew_sessions(
    request: Request,
    state: str | None = Query(default=None, description="a presence state, or 'live'"),
    access: CrewAccess = Depends(crew_access("crew:read")),
) -> Any:
    try:
        sessions = await _service(request).list_sessions(access.crew_id, state)
    except SessionError as e:
        raise _session_error(e)
    return JSONResponse({"crew_id": access.crew_id, "sessions": sessions})


__all__ = ["router", "authenticate_host"]
