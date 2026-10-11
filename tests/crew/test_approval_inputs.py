"""Unregistered transport boundary: no rejected authority reaches errors/logs."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from remembra.crew.approval_inputs import (
    INVALID_INPUT,
    MAX_INPUT_BYTES,
    ApprovalInputError,
    parse_consume,
    parse_decision,
    parse_request,
    parse_revoke,
)

DIGEST = "a" * 64
SECRET = "ghp_" + "x" * 36


def request_body() -> dict[str, Any]:
    return {
        "operation": {
            "action": "repository.apply_patch",
            "resource_type": "repository",
            "resource_id": "repo-1",
            "scope": ["src/example.py"],
            "base_revision": "a" * 40,
            "candidate_revision": "b" * 40,
            "parameters_json": "{}",
        },
        "expires_at_ms": 42,
        "idempotency_key": "request-1",
    }


PARSERS: list[tuple[Callable[..., Any], dict[str, Any]]] = [
    (parse_request, request_body()),
    (parse_decision, {"request_digest": DIGEST, "expected_version": 1, "approve": True}),
    (parse_revoke, {"request_digest": DIGEST, "expected_version": 1}),
    (parse_consume, {"request_digest": DIGEST}),
]


@pytest.mark.parametrize(("parser", "body"), PARSERS)
def test_closed_body_accepts_json_types(parser: Callable[..., Any], body: dict[str, Any]) -> None:
    result = parser(json.dumps(body).encode())
    assert result.model_dump(mode="json") == body


@pytest.mark.parametrize(("parser", "body"), PARSERS)
@pytest.mark.parametrize("authority", ["principal", "executor", "tenant_id", "auth_time_ms"])
def test_body_cannot_supply_authority(parser: Callable[..., Any], body: dict[str, Any], authority: str) -> None:
    with pytest.raises(ApprovalInputError, match=INVALID_INPUT):
        parser(json.dumps({**body, authority: SECRET}).encode())


@pytest.mark.parametrize("field", ["action", "resource_type", "resource_id", "scope", "parameters_json", "idempotency_key"])
def test_credentials_rejected_without_error_context_or_traceback_leak(field: str) -> None:
    body = request_body()
    if field == "idempotency_key":
        body[field] = SECRET
    else:
        body["operation"][field] = (
            [SECRET]
            if field == "scope"
            else json.dumps({"token": SECRET}, separators=(",", ":"))
            if field == "parameters_json"
            else SECRET
        )
    with pytest.raises(ApprovalInputError) as error:
        parse_request(json.dumps(body).encode())
    assert str(error.value) == INVALID_INPUT
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert SECRET not in repr(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"{} trailing",
        b"{",
        b"[]",
        b"null",
        b'{"request_digest":"' + DIGEST.encode() + b'","request_digest":"' + DIGEST.encode() + b'"}',
        b'{"request_digest": NaN}',
        b'{"request_digest": Infinity}',
        b'"' + b"x" * MAX_INPUT_BYTES + b'"',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_malformed_ambiguous_or_unbounded_input_fails_closed(body: bytes) -> None:
    with pytest.raises(ApprovalInputError, match=INVALID_INPUT):
        parse_consume(body)


@pytest.mark.parametrize(
    "replacement", [{"approve": "true"}, {"approve": 1}, {"expected_version": True}, {"expected_version": "1"}]
)
def test_decisions_do_not_coerce_authority(replacement: dict[str, Any]) -> None:
    body = {"request_digest": DIGEST, "expected_version": 1, "approve": True, **replacement}
    with pytest.raises(ApprovalInputError):
        parse_decision(json.dumps(body).encode())


def test_duplicate_nested_operation_key_is_rejected() -> None:
    body = json.dumps(request_body()).replace('"resource_id": "repo-1"', '"resource_id": "repo-1", "resource_id": "repo-2"')
    with pytest.raises(ApprovalInputError):
        parse_request(body.encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["field", "extra_key", "malformed"])
async def test_synthetic_http_error_and_standard_exception_log_never_echo_input(
    caplog: pytest.LogCaptureFixture, location: str
) -> None:
    # No real approval route is registered. Authentication, stream byte limits
    # and action-policy integration remain requirements for the production slice.
    app = FastAPI()

    @app.post("/test-input")
    async def synthetic_boundary(request: Request) -> JSONResponse:
        try:
            parse_request(await request.body())
        except ApprovalInputError:
            logging.getLogger("approval-input-test").exception("approval_input_rejected")
            return JSONResponse({"detail": INVALID_INPUT}, status_code=422)
        return JSONResponse({"accepted": True})

    body = request_body()
    if location == "field":
        body["idempotency_key"] = SECRET
    elif location == "extra_key":
        body[SECRET] = "untrusted"
    payload = json.dumps(body).encode() if location != "malformed" else ("{" + SECRET).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/test-input", content=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_INPUT}
    assert "approval_input_rejected" in caplog.text
    assert SECRET not in response.text + caplog.text
