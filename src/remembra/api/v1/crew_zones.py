"""Crew zone routes (WP-5, spec §6 "Zones").

Every route resolves its crew through ``crew/access.py`` (``crew_access`` for
``/crews/{crew_id}/…``, ``crew_entity`` for ``/zones/{id}`` and ``/zone-changes/{id}``), so a
foreign or unknown id is a 404. (H) routes accept a dashboard login only (D27). Agents act as a
crew session: API-key callers send the session token in ``X-Remembra-Crew-Session``.

The helpers at the top are shared with :mod:`remembra.api.v1.crew_claims`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from remembra.crew import schemas as S
from remembra.crew import zones as Z
from remembra.crew.access import CrewAccess, CrewEntity, crew_access, crew_entity, crew_error, not_found
from remembra.crew.claims import SESSION_HEADER, authenticate_session
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, IdempotencyConflict, TokenBearingResponse, idem_lookup, idem_store
from remembra.crew.limits import crew_limits_for_owner
from remembra.crew.store import NotFound

router = APIRouter(tags=["crew-zones"])

PR, PW, PO, PA = "crew:read", "crew:write", "crew:override", "crew:admin"


# ---------------------------------------------------------------------------
# Shared route helpers
# ---------------------------------------------------------------------------


def crew_ops(request: Request) -> Z.CrewOps:
    """Event log (crew.db + the in-process bus), main-DB audit sink. 503 when crew mode is not running."""
    db = getattr(request.app.state, "crew_db", None)
    if not isinstance(db, CrewDatabase):
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    event_log = getattr(request.app.state, "crew_events", None)
    if not isinstance(event_log, CrewEventLog):
        event_log = CrewEventLog(db, getattr(request.app.state, "crew_bus", None))
    main_db = getattr(request.app.state, "db", None)
    sink = Z.main_db_audit_sink(main_db) if main_db is not None and hasattr(main_db, "log_audit_event") else None
    return Z.CrewOps(event_log, sink)


async def with_limits(request: Request, ops: Z.CrewOps, owner_user_id: str) -> Z.CrewOps:
    ops.limits = await crew_limits_for_owner(getattr(request.app.state, "usage_meter", None), owner_user_id)
    return ops


@asynccontextmanager
async def crew_errors() -> AsyncIterator[None]:
    """Map service errors to the ``CrewError`` body (§6)."""
    try:
        yield
    except Z.CrewOpError as e:
        raise HTTPException(status_code=e.status, detail=e.body()) from e
    except NotFound as e:
        raise not_found() from e


async def principal_for(request: Request, access: CrewAccess) -> Z.Principal:
    """A human for a dashboard login; otherwise the crew session named by the session-token header."""
    if access.human:
        return Z.Principal.human(access.user.user_id, api_key_id=access.user.api_key_id)
    async with crew_errors():
        session = await authenticate_session(
            access_conn(request), request.headers.get(SESSION_HEADER), user_id=access.user.user_id, crew_id=access.crew_id
        )
    return Z.Principal.for_session(session, api_key_id=access.user.api_key_id)


def access_conn(request: Request) -> Any:
    db = request.app.state.crew_db
    return getattr(db, "conn", db)


async def read_body(request: Request, shape: str | None = None, *, required: bool = True) -> dict[str, Any]:
    """The JSON object body; validated against ``schemas.REQUEST_SHAPES[shape]`` (closed objects) when named."""
    raw = await request.body()
    if not raw:
        if required and shape is not None:
            raise crew_error(422, "invalid_body", "A JSON body is required.")
        return {}
    try:
        body = json.loads(raw)
    except ValueError as e:
        raise crew_error(422, "invalid_body", "The body is not valid JSON.") from e
    if not isinstance(body, dict):
        raise crew_error(422, "invalid_body", "The body must be a JSON object.")
    if shape is not None:
        errors = S.validate(body, S.REQUEST_SHAPES[shape])
        if errors:
            raise crew_error(422, "invalid_body", "The body is invalid.", problems=errors[:20])
    return body


def closed(body: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    unknown = set(body) - allowed
    if unknown:
        raise crew_error(422, "invalid_body", f"Unknown field(s): {', '.join(sorted(unknown))}.")
    return body


async def idempotent(
    request: Request,
    ops: Z.CrewOps,
    principal: Z.Principal,
    route: str,
    body: Any,
    fn: Callable[[], Awaitable[Any]],
    *,
    atomic: bool = True,
) -> Any:
    """Honour ``Idempotency-Key`` (72 h, per principal and route). With ``atomic`` the mutation and the stored
    response commit in one transaction; token-bearing responses are never stored (§4.3)."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return await fn()
    if len(key) > 200:
        raise crew_error(422, "invalid_idempotency_key", "Idempotency-Key is at most 200 characters.")
    who = principal.session_id or f"user:{principal.user_id}"
    try:
        if atomic:
            async with ops.log.transaction() as tx:
                stored = await idem_lookup(tx.conn, principal=who, route=route, key=key, body=body)
                if stored is not None:
                    return stored
                result = await fn()
                if isinstance(result, dict):
                    try:
                        await idem_store(tx.conn, principal=who, route=route, key=key, body=body, response=result)
                    except TokenBearingResponse:
                        pass
                return result
        stored = await idem_lookup(ops.db.conn, principal=who, route=route, key=key, body=body)
        if stored is not None:
            return stored
        result = await fn()
        if isinstance(result, dict):
            async with ops.log.transaction() as tx:
                try:
                    await idem_store(tx.conn, principal=who, route=route, key=key, body=body, response=result)
                except TokenBearingResponse:
                    pass
        return result
    except IdempotencyConflict as e:
        raise crew_error(422, "idempotency_conflict", "Idempotency-Key was reused with a different request body.") from e


def if_match_version(request: Request) -> int:
    raw = (request.headers.get("If-Match") or "").strip().strip('"').removeprefix("W/").strip('"')
    if not raw:
        raise crew_error(status.HTTP_428_PRECONDITION_REQUIRED, "if_match_required", "Send If-Match with the zone version.")
    try:
        return int(raw)
    except ValueError as e:
        raise crew_error(412, "version_mismatch", "If-Match must be the zone version number.") from e


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/zones")
async def list_zones(request: Request, access: CrewAccess = Depends(crew_access(PR))) -> dict[str, Any]:
    conn = access_conn(request)
    rows = await Z.load_zone_rows(conn, access.crew_id)
    return {
        "zones": [Z.zone_detail(r) for r in rows],
        "commons": await Z.active_commons(conn, access.crew_id),
        "ignore": await Z.active_ignore(conn, access.crew_id),
        "bootstrap_zones": await Z.bootstrap_active(conn, access.crew_id),
        "pending_zone_changes": await Z.list_pending_change_ids(conn, access.crew_id),
    }


@router.post("/crews/{crew_id}/zones", status_code=201)
async def create_zone(request: Request, access: CrewAccess = Depends(crew_access(PW))) -> Any:
    principal = await principal_for(request, access)
    body = closed(await read_body(request), set(Z.ZONE_BODY_KEYS))
    ops = await with_limits(request, crew_ops(request), access.crew.owner_user_id)
    async with crew_errors():
        return await idempotent(
            request, ops, principal, "zones.create", body, lambda: Z.create_zone(ops, access.crew_id, principal, body)
        )


@router.patch("/zones/{zone_id}")
async def patch_zone(request: Request, ent: CrewEntity = Depends(crew_entity("zone", PW))) -> Any:
    principal = await principal_for(request, ent.access)
    version = if_match_version(request)
    body = closed(await read_body(request), set(Z.ZONE_BODY_KEYS))
    ops = crew_ops(request)
    async with crew_errors():
        return await Z.patch_zone(ops, ent.row, principal, body, if_match=version)


@router.delete("/zones/{zone_id}")
async def delete_zone(request: Request, ent: CrewEntity = Depends(crew_entity("zone", PW))) -> Any:
    principal = await principal_for(request, ent.access)
    ops = crew_ops(request)
    async with crew_errors():
        return await Z.archive_zone(ops, ent.row, principal)


@router.put("/crews/{crew_id}/zones/file")
async def put_zones_file(request: Request, access: CrewAccess = Depends(crew_access(PW))) -> Any:
    principal = await principal_for(request, access)
    body = await read_body(request, "ZonesFile")
    ops = await with_limits(request, crew_ops(request), access.crew.owner_user_id)
    async with crew_errors():
        return await idempotent(
            request,
            ops,
            principal,
            "zones.file",
            body,
            lambda: Z.upload_zones_file(
                ops, access.crew_id, principal, yaml_text=body["yaml"], sha=body["sha"], branch=body["branch"]
            ),
        )


@router.get("/crews/{crew_id}/zone-changes")
async def list_zone_changes(
    request: Request, state: str | None = Query(default=None), access: CrewAccess = Depends(crew_access(PR))
) -> dict[str, Any]:
    if state is not None and state not in ("pending", "applied", "rejected"):
        raise crew_error(422, "invalid_state", "state must be pending, applied or rejected.")
    return {"changes": await Z.list_zone_changes(access_conn(request), access.crew_id, state)}


async def _decide(request: Request, ent: CrewEntity, approve: bool) -> Any:
    human = Z.Principal.human(ent.access.user.user_id, api_key_id=ent.access.user.api_key_id)
    ops = await with_limits(request, crew_ops(request), ent.access.crew.owner_user_id)
    async with crew_errors():
        return await Z.decide_zone_change(ops, ent.row, human, approve=approve)


@router.post("/zone-changes/{change_id}/approve")
async def approve_zone_change(request: Request, ent: CrewEntity = Depends(crew_entity("zone_change", PA, step_up=True))) -> Any:
    return await _decide(request, ent, True)


@router.post("/zone-changes/{change_id}/reject")
async def reject_zone_change(request: Request, ent: CrewEntity = Depends(crew_entity("zone_change", PA))) -> Any:
    return await _decide(request, ent, False)


@router.get("/crews/{crew_id}/zones/export")
async def export_zones(request: Request, access: CrewAccess = Depends(crew_access(PR))) -> dict[str, Any]:
    return await Z.export(access_conn(request), access.crew_id)


@router.post("/crews/{crew_id}/zones/suggest")
async def suggest_zones(request: Request, access: CrewAccess = Depends(crew_access(PW))) -> Any:
    body = closed(await read_body(request, required=False), {"tree", "churn"})
    churn = body.get("churn")
    if churn is not None and (
        not isinstance(churn, dict)
        or len(churn) > 400
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in churn.values())
    ):
        raise crew_error(422, "invalid_body", "churn must map folder paths to integers (at most 400).")
    async with crew_errors():
        return await Z.suggest(access_conn(request), access.crew_id, tree=body.get("tree"), churn=churn)


@router.put("/crews/{crew_id}/tree")
async def put_tree(request: Request, access: CrewAccess = Depends(crew_access(PW))) -> Any:
    principal = await principal_for(request, access)
    body = await read_body(request, "Tree")
    ops = await with_limits(request, crew_ops(request), access.crew.owner_user_id)
    async with crew_errors():
        return await Z.put_tree(ops, access.crew_id, principal, body["tree"])


@router.post("/zones/{zone_id}/freeze")
async def freeze_zone(request: Request, ent: CrewEntity = Depends(crew_entity("zone", PO))) -> Any:
    body = await read_body(request, "Freeze")
    human = Z.Principal.human(ent.access.user.user_id, api_key_id=ent.access.user.api_key_id)
    ops = crew_ops(request)
    async with crew_errors():
        return await Z.freeze_zone(ops, ent.row, human, reason=body["reason"], until=body.get("until"))


@router.post("/zones/{zone_id}/unfreeze")
async def unfreeze_zone(request: Request, ent: CrewEntity = Depends(crew_entity("zone", PO, step_up=True))) -> Any:
    body = await read_body(request, "Reason")
    human = Z.Principal.human(ent.access.user.user_id, api_key_id=ent.access.user.api_key_id)
    ops = crew_ops(request)
    async with crew_errors():
        return await Z.unfreeze_zone(ops, ent.row, human, reason=body["reason"])


@router.post("/crews/{crew_id}/match")
async def match_paths(request: Request, access: CrewAccess = Depends(crew_access(PR))) -> Any:
    body = await read_body(request, "Match")
    return await Z.match(
        access_conn(request),
        access.crew_id,
        paths=body["paths"],
        command_tokens=body.get("command_tokens"),
        mcp_tool=body.get("mcp_tool"),
    )


def json_response(status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)
