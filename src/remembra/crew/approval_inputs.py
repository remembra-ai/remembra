"""Unregistered HTTP input boundary for scoped approvals.

Future routes must authenticate first, bound the streamed body before buffering,
and call these parsers instead of automatic body-model validation. Rejections
contain only a fixed error, never Pydantic error details or an input value. Do not
log request bodies, raw validation errors or traceback locals. This module does
not register an API, derive authority, validate action semantics or grant access.
"""

from __future__ import annotations

import json
from typing import Annotated, TypeVar

from pydantic import Field, ValidationError

from remembra.crew.approval_contracts import Contract, Digest, Identifier, Operation

MAX_INPUT_BYTES = 32_768
INVALID_INPUT = "Invalid approval input."


class ApprovalInputError(ValueError):
    """Safe rejection with no input, validation details or exception chain."""

    def __init__(self) -> None:
        super().__init__(INVALID_INPUT)


class RequestInput(Contract):
    """Authority and intended executor come from authentication, never this body."""

    operation: Operation
    expires_at_ms: Annotated[int, Field(gt=0)]
    idempotency_key: Identifier


class DecisionInput(Contract):
    """Bind a fresh human decision to the displayed request and version."""

    request_digest: Digest
    expected_version: Annotated[int, Field(gt=0)]
    approve: bool


class RevokeInput(Contract):
    """Bind revocation to the displayed request and version."""

    request_digest: Digest
    expected_version: Annotated[int, Field(gt=0)]


class ConsumeInput(Contract):
    """Identity, session and execution policy must be revalidated by the service."""

    request_digest: Digest


Input = TypeVar("Input", bound=Contract)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError


def _parse(body: bytes, model: type[Input]) -> Input:
    # Do not attach or format a caught exception: even .errors() locations can
    # contain attacker-chosen keys. Raise outside except to discard its context.
    if len(body) <= MAX_INPUT_BYTES:
        try:
            text = body.decode("utf-8")
            json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            return model.model_validate_json(text)
        except (ValueError, ValidationError, RecursionError):
            pass
    raise ApprovalInputError()


def parse_request(body: bytes) -> RequestInput:
    """Validate request fields without exposing rejected input in error output."""
    return _parse(body, RequestInput)


def parse_decision(body: bytes) -> DecisionInput:
    """Validate decision fields; this alone confers no human authority."""
    return _parse(body, DecisionInput)


def parse_revoke(body: bytes) -> RevokeInput:
    """Validate revocation fields without exposing rejected input."""
    return _parse(body, RevokeInput)


def parse_consume(body: bytes) -> ConsumeInput:
    """Validate claim fields; a successful parse is not execution permission."""
    return _parse(body, ConsumeInput)
