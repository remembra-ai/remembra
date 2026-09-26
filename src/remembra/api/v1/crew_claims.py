"""Crew claim, guard, bypass-code and collision routes (WP-5, spec §6 "Claims and guard", "Collisions").

Agents act as their crew session (``X-Remembra-Crew-Session`` token); a dashboard login acts as
a human. Human-only routes (override, bypass codes, dismiss) refuse API keys (D27); override and
bypass codes also need a login within 15 minutes. ``guard`` and ``claims`` use their own reserved
rate buckets, ``adopt`` its own (§11).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from remembra.api.v1.crew_zones import (
    access_conn,
    closed,
    crew_errors,
    crew_ops,
    idempotent,
    json_response,
    principal_for,
    read_body,
)
from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.crew import bypass as B
from remembra.crew import claims as C
from remembra.crew import collisions as CO
from remembra.crew import zones as Z
from remembra.crew.access import CrewAccess, CrewEntity, crew_access, crew_entity, crew_error, load_crew
from remembra.crew.limits import enforce_rate_limit

router = APIRouter(tags=["crew-claims"])

PR, PW, PC, PO = "crew:read", "crew:write", "crew:claim", "crew:override"


def _limit(bucket: str, request: Request, principal: Z.Principal) -> None:
    token = request.headers.get(C.SESSION_HEADER) if principal.session is not None else None
    enforce_rate_limit(bucket, user_id=principal.user_id, session_token=token)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@router.post("/crews/{crew_id}/claims")
async def create_claim(request: Request, access: CrewAccess = Depends(crew_access(PC))) -> Any:
    principal = await principal_for(request, access)
    _limit("claims", request, principal)
    body = await read_body(request, "Claim")
    ops = crew_ops(request)

    async def run() -> dict[str, Any]:
        outcome = await C.request_claim(
            ops,
            access.crew_id,
            principal,
            zone_id=body.get("zone_id"),
            path_glob=body.get("path_glob"),
            resource=body.get("resource"),
            mode=body["mode"],
            task_id=body.get("task_id"),
            reason=body.get("reason"),
            wait=bool(body["wait"]),
            source=body["source"],
        )
        return {"http_status": outcome.http_status, **outcome.body()}

    async with crew_errors():
        result = await idempotent(request, ops, principal, "claims.create", body, run, atomic=False)
        wait_s = int(body.get("wait_s") or 0)
        claim = result.get("claim")
        if result.get("status") == "queued" and wait_s > 0 and claim:
            row = await C.wait_for_claim(ops, str(claim["id"]), wait_s)
            result = {
                "http_status": 201 if row["state"] == "active" else 202,
                "status": "granted" if row["state"] == "active" else row["state"],
                "claim": C.claim_view(row),
                "blockers": result.get("blockers", []),
                "waited_s": wait_s if row["state"] == "queued" else None,
            }
    code = int(result.pop("http_status", 200))
    if code == 409:
        return json_response(409, {"detail": result})
    return json_response(code, result)


@router.get("/crews/{crew_id}/claims")
async def get_claims(
    request: Request, state: str | None = Query(default=None), access: CrewAccess = Depends(crew_access(PR))
) -> Any:
    async with crew_errors():
        return {"claims": await C.list_claims(access_conn(request), access.crew_id, state)}


@router.post("/claims/{claim_id}/release")
async def release_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PC))) -> Any:
    principal = await principal_for(request, ent.access)
    _limit("claims", request, principal)
    body = closed(await read_body(request, required=False), {"baton", "note"})
    if "baton" in body and not isinstance(body["baton"], bool):
        raise crew_error(422, "invalid_body", "baton must be true or false.")
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request,
            ops,
            principal,
            "claims.release",
            {**body, "id": ent.id},
            lambda: C.release_claim(ops, ent.row, principal, baton=bool(body.get("baton")), note=body.get("note")),
        )


@router.post("/claims/{claim_id}/handover")
async def handover_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PC))) -> Any:
    principal = await principal_for(request, ent.access)
    _limit("claims", request, principal)
    body = closed(await read_body(request), {"to", "note"})
    if not isinstance(body.get("to"), str):
        raise crew_error(422, "invalid_body", "to (a session id) is required.")
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request,
            ops,
            principal,
            "claims.handover",
            {**body, "id": ent.id},
            lambda: C.handover(ops, ent.row, principal, to=body["to"], note=body.get("note")),
        )


@router.post("/claims/{claim_id}/accept")
async def accept_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PC))) -> Any:
    principal = await principal_for(request, ent.access)
    _limit("claims", request, principal)
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request, ops, principal, "claims.accept", {"id": ent.id}, lambda: C.accept_handover(ops, ent.row, principal)
        )


@router.post("/claims/{claim_id}/decline")
async def decline_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PC))) -> Any:
    principal = await principal_for(request, ent.access)
    _limit("claims", request, principal)
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request, ops, principal, "claims.decline", {"id": ent.id}, lambda: C.decline_handover(ops, ent.row, principal)
        )


@router.post("/claims/{claim_id}/adopt")
async def adopt_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PC))) -> Any:
    principal = await principal_for(request, ent.access)
    _limit("adopt", request, principal)
    body = closed(await read_body(request, required=False), {"task_id"})
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request,
            ops,
            principal,
            "claims.adopt",
            {**body, "id": ent.id},
            lambda: C.adopt(ops, ent.row, principal, task_id=body.get("task_id")),
        )


@router.post("/claims/{claim_id}/override")
async def override_claim(request: Request, ent: CrewEntity = Depends(crew_entity("claim", PO, step_up=True))) -> Any:
    body = await read_body(request, "Override")
    human = Z.Principal.human(ent.access.user.user_id, api_key_id=ent.access.user.api_key_id)
    ops = crew_ops(request)
    async with crew_errors():
        return await idempotent(
            request,
            ops,
            human,
            "claims.override",
            {**body, "id": ent.id},
            lambda: C.override(ops, ent.row, human, action=body["action"], to=body.get("to"), reason=body["reason"]),
        )


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@router.post("/crews/{crew_id}/guard")
async def guard(request: Request, access: CrewAccess = Depends(crew_access(PC))) -> Any:
    principal = await principal_for(request, access)
    if principal.session is None:
        raise crew_error(422, "session_required", "The guard decides for a crew session (send its token).")
    _limit("guard", request, principal)
    body = await read_body(request, "Guard")
    if body["session_id"] != principal.session_id:
        raise crew_error(422, "session_mismatch", "session_id must be the session of the token.")
    ops = crew_ops(request)
    async with crew_errors():
        return await C.server_guard(
            ops,
            access.crew_id,
            principal,
            op=body["op"],
            paths=body["paths"],
            command_tokens=body.get("command_tokens"),
            mcp_tool=body.get("mcp_tool"),
        )


# ---------------------------------------------------------------------------
# Bypass codes (D34)
# ---------------------------------------------------------------------------


@router.post("/crews/{crew_id}/bypass-codes", status_code=201)
async def issue_bypass_code(request: Request, access: CrewAccess = Depends(crew_access(PO, step_up=True))) -> Any:
    body = await read_body(request, "BypassIssue")
    human = Z.Principal.human(access.user.user_id, api_key_id=access.user.api_key_id)
    ops = crew_ops(request)
    async with crew_errors():
        return await B.issue_code(
            ops, access.crew_id, human, session_id=body["session_id"], scope=body["scope"], minutes=body["minutes"]
        )


@router.post("/bypass-codes/redeem")
async def redeem_bypass_code(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> Any:
    body = await read_body(request, "BypassRedeem")
    crew_ops(request)  # 503 when crew mode is not running
    conn = access_conn(request)
    async with crew_errors():
        session = await C.authenticate_session(
            conn,
            request.headers.get(C.SESSION_HEADER),
            user_id=user.user_id,
            session_id=body["session_id"],
            agent_id=user.agent_id,
        )
    await load_crew(conn, session["crew_id"], user, PC)
    enforce_rate_limit("claims", user_id=user.user_id, session_token=request.headers.get(C.SESSION_HEADER))
    ops = crew_ops(request)
    async with crew_errors():
        return await B.redeem_code(ops, session, body["code"])


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/collisions")
async def get_collisions(
    request: Request, state: str | None = Query(default=None), access: CrewAccess = Depends(crew_access(PR))
) -> Any:
    async with crew_errors():
        return {"collisions": await CO.list_collisions(access_conn(request), access.crew_id, state)}


@router.post("/collisions/{collision_id}/ack")
async def ack_collision(request: Request, ent: CrewEntity = Depends(crew_entity("collision", PW))) -> Any:
    principal = await principal_for(request, ent.access)
    ops = crew_ops(request)
    async with crew_errors():
        return await CO.acknowledge(ops, ent.row, principal)


@router.post("/collisions/{collision_id}/resolve")
async def resolve_collision(request: Request, ent: CrewEntity = Depends(crew_entity("collision", PW))) -> Any:
    principal = await principal_for(request, ent.access)
    body = closed(await read_body(request, required=False), {"resolution"})
    ops = crew_ops(request)
    async with crew_errors():
        return await CO.resolve(ops, ent.row, principal, str(body.get("resolution") or "resolved"))


@router.post("/collisions/{collision_id}/dismiss")
async def dismiss_collision(request: Request, ent: CrewEntity = Depends(crew_entity("collision", PO))) -> Any:
    body = closed(await read_body(request, required=False), {"reason"})
    human = Z.Principal.human(ent.access.user.user_id, api_key_id=ent.access.user.api_key_id)
    ops = crew_ops(request)
    async with crew_errors():
        return await CO.dismiss(ops, ent.row, human, body.get("reason"))
