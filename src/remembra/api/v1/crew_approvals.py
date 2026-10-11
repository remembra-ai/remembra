"""Unregistered approval transport for independent integration review.

Importing this module installs nothing. A caller must provide the reviewed
service and a mandatory rate gate; there is no production persistence, policy,
rate-bucket selection, route wiring or execution/resume fallback here.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.crew.access import HUMAN_CREDENTIAL_ID, crew_error, jwt_auth_time_ms, load_crew
from remembra.crew.approval_contracts import ApprovalRecord
from remembra.crew.approval_inputs import (
    INVALID_INPUT,
    MAX_INPUT_BYTES,
    ApprovalInputError,
    parse_decision,
    parse_request,
    parse_revoke,
)
from remembra.crew.approvals import ApprovalError, CrewApprovals
from remembra.crew.claims import SESSION_HEADER, authenticate_session
from remembra.crew.zones import CrewOpError

Action = Literal["request", "get", "decide", "revoke"]
RateGate = Callable[[Action, AuthenticatedUser, str | None], None]
BODY_TIMEOUT_SECONDS = 5


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _rate_limit(rate_gate: RateGate, action: Action, user: AuthenticatedUser, token: str | None) -> JSONResponse | None:
    try:
        # An annotation cannot enforce this mandatory synchronous port at runtime.
        result = cast(Callable[[Action, AuthenticatedUser, str | None], object], rate_gate)(action, user, token)
        if result is None:
            return None
        if inspect.iscoroutine(result):
            result.close()
        elif isinstance(result, asyncio.Future):
            result.cancel()
    except HTTPException as error:
        if error.status_code == 429:
            return _error(429, "rate_limited", "Approval rate limit reached.")
    except Exception:
        pass
    # Never await unexpected results, or echo adapter errors, headers or inputs.
    return _error(503, "approval_unavailable", "Approvals are temporarily unavailable.")


async def _body(request: Request) -> bytes:
    # Never call request.body(): reject before extending the bounded buffer.
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (lengths and not (lengths[0].isascii() and lengths[0].isdecimal())):
        raise crew_error(400, "invalid_length", "Invalid approval body length.")
    if lengths and (len(lengths[0]) > 10 or int(lengths[0]) > MAX_INPUT_BYTES):
        raise crew_error(413, "body_too_large", "Approval body is too large.")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json" or request.headers.get("content-encoding", "identity") != "identity":
        raise crew_error(415, "unsupported_media_type", "Approval body must be uncompressed JSON.")
    result = bytearray()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(chunk) > MAX_INPUT_BYTES - len(result):
                    raise crew_error(413, "body_too_large", "Approval body is too large.")
                result.extend(chunk)
    except TimeoutError:
        raise crew_error(408, "body_timeout", "Approval body timed out.") from None
    except ClientDisconnect:
        raise crew_error(400, "body_disconnected", "Approval body was interrupted.") from None
    if lengths and int(lengths[0]) != len(result):
        raise crew_error(400, "invalid_length", "Invalid approval body length.")
    return bytes(result)


def _view(record: ApprovalRecord) -> JSONResponse:
    # This router has no consume route or host adapter. Keep decision state and
    # actual execution support separate; a response is not an execution receipt.
    return JSONResponse(
        {
            "approval": record.model_dump(mode="json"),
            "request_digest": record.request.digest,
            "execution": {"supported": False, "reason": "unsupported_adapter"},
        }
    )


def make_approval_router(service: CrewApprovals, *, rate_gate: RateGate) -> APIRouter:
    """Build request/get/decision/revoke handlers without registering them.

    The synchronous rate gate must return None or raise to deny. It must use
    reviewed existing Crew limits; bucket mapping is an explicit integration
    decision, never an agent input. Async adapters and other results fail closed.
    """
    # Inspect async method semantics separately from the callable() check below.
    call = getattr(rate_gate, "__call__", None)  # noqa: B004
    if (
        not callable(rate_gate)
        or inspect.iscoroutinefunction(rate_gate)
        or inspect.iscoroutinefunction(call)
        or inspect.isasyncgenfunction(rate_gate)
        or inspect.isasyncgenfunction(call)
    ):
        raise TypeError("Approval rate gate must be synchronous.") from None
    router = APIRouter(tags=["crew-approvals"])

    async def authorize(request: Request, crew_id: str, action: Action) -> AuthenticatedUser:
        user = await get_current_user(request, request.headers.get("x-api-key"))
        human = action in ("decide", "revoke")
        if human and user.api_key_id != HUMAN_CREDENTIAL_ID:
            raise crew_error(403, "human_only", "This action needs a dashboard login.")
        if service.store.in_transaction:
            raise crew_error(503, "approval_unavailable", "Approvals are temporarily unavailable.")
        # Preflight prevents invisible callers and invalid sessions from causing
        # body reads. The service repeats authority checks in its own commit.
        async with service.store.transaction() as tx:
            await load_crew(
                tx.conn,
                crew_id,
                user,
                "crew:override" if human else "crew:write" if action == "request" else "crew:read",
                step_up=human,
                auth_time_ms=jwt_auth_time_ms(request, user),
                now_ms=service.clock(),
            )
            if action == "request":
                await authenticate_session(
                    tx.conn,
                    request.headers.get(SESSION_HEADER),
                    user_id=user.user_id,
                    crew_id=crew_id,
                    agent_id=user.agent_id,
                )
        return user

    async def dispatch(request: Request, crew_id: str, action: Action, approval_id: str = "") -> JSONResponse:
        try:
            user = await authorize(request, crew_id, action)
            limited = _rate_limit(rate_gate, action, user, request.headers.get(SESSION_HEADER) if action == "request" else None)
            if limited is not None:
                return limited
            if action == "get":
                return _view(await service.get(crew_id=crew_id, approval_id=approval_id, user=user))
            body = await _body(request)
            # Streaming can outlive a key, login, membership or session. Do not
            # reuse the earlier principal after receiving the complete body.
            user = await authorize(request, crew_id, action)
            if action == "request":
                value = parse_request(body)
                record = await service.request(
                    crew_id=crew_id,
                    user=user,
                    session_token=request.headers.get(SESSION_HEADER),
                    operation=value.operation,
                    expires_at_ms=value.expires_at_ms,
                    idempotency_key=value.idempotency_key,
                )
            elif action == "decide":
                decision = parse_decision(body)
                record = await service.decide(
                    crew_id=crew_id,
                    approval_id=approval_id,
                    user=user,
                    auth_time_ms=jwt_auth_time_ms(request, user),
                    request_digest=decision.request_digest,
                    expected_version=decision.expected_version,
                    approve=decision.approve,
                )
            else:
                revocation = parse_revoke(body)
                record = await service.revoke(
                    crew_id=crew_id,
                    approval_id=approval_id,
                    user=user,
                    auth_time_ms=jwt_auth_time_ms(request, user),
                    request_digest=revocation.request_digest,
                    expected_version=revocation.expected_version,
                )
            return _view(record)
        except CrewOpError as error:
            code = error.status if error.status in (401, 403, 404, 409) else 503
            return _error(code, "approval_rejected", "Approval request rejected.")
        except ApprovalInputError:
            return _error(422, "invalid_input", INVALID_INPUT)
        except ApprovalError:
            # Policy adapters may choose their own exception code: never echo it.
            return _error(409, "approval_conflict", "Approval request rejected.")
        except ValidationError:
            # Server-derived authority references can fail the closed contracts
            # too. Raw Pydantic errors contain inputs and must never reach logs.
            return _error(503, "approval_unavailable", "Approvals are temporarily unavailable.")

    @router.post("/crews/{crew_id}/approvals")
    async def request_approval(request: Request, crew_id: str) -> JSONResponse:
        return await dispatch(request, crew_id, "request")

    @router.get("/crews/{crew_id}/approvals/{approval_id}")
    async def get_approval(request: Request, crew_id: str, approval_id: str) -> JSONResponse:
        return await dispatch(request, crew_id, "get", approval_id)

    @router.post("/crews/{crew_id}/approvals/{approval_id}/decide")
    async def decide_approval(request: Request, crew_id: str, approval_id: str) -> JSONResponse:
        return await dispatch(request, crew_id, "decide", approval_id)

    @router.post("/crews/{crew_id}/approvals/{approval_id}/revoke")
    async def revoke_approval(request: Request, crew_id: str, approval_id: str) -> JSONResponse:
        return await dispatch(request, crew_id, "revoke", approval_id)

    return router
