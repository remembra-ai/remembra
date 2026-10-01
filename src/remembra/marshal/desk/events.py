"""The desk's server-sent events (``POST /api/v1/marshal/ask``).

Framing is ``event: <name>`` then one ``data:`` line of compact JSON and a
blank line; ``: keepalive`` comments keep an idle stream open. Every stream is
``read`` (0 to 4), then exactly one ``answer`` or ``error``, then one
``usage``, then one ``done``.
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict

KEEPALIVE = b": keepalive\n\n"


class ReadEvent(TypedDict):
    id: str
    tool: str
    label: str
    args: dict[str, Any]
    ok: bool
    http_status: int | None
    summary: str
    ms: int
    anchor: str | None


class EvidenceRef(TypedDict):
    ref: str
    label: str
    anchor: str | None


class CommandLine(TypedDict):
    text: str
    kind: Literal["terminal", "codex_ui", "agent"]
    prompt: Literal["$", ">"]


class AnswerEvent(TypedDict):
    text: str
    evidence: list[EvidenceRef]
    commands: list[CommandLine]
    fallback: bool
    fallback_reason: str | None
    summary: list[str]
    doc: str | None


class UsageEvent(TypedDict):
    model: str
    reads: int
    model_calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    usd: float
    billed_to_credits: bool
    asks_today: int
    asks_limit: int
    input_redactions: int


ErrorCode = Literal["model_unavailable", "timeout", "internal"]


class ErrorEvent(TypedDict):
    error: ErrorCode
    message: str
    retryable: bool


class DoneEvent(TypedDict):
    ok: bool


ERROR_MESSAGES: dict[str, str] = {
    "model_unavailable": "Marshal's model didn't answer. Rules-only checks still work.",
    "timeout": "Marshal took too long to answer. Ask again.",
    "internal": "Marshal hit an error. Ask again.",
}


def sse(event: str, data: Any) -> bytes:
    """One event: ``event: <name>\\ndata: <compact json>\\n\\n`` (the JSON never spans lines)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def error_event(code: ErrorCode) -> ErrorEvent:
    return {"error": code, "message": ERROR_MESSAGES[code], "retryable": True}
