"""The Marshal desk: the dashboard's read-only copilot on Remembra's own model (spec M3).

- ``GET  /api/v1/marshal/board``     what needs a look, by rules only (no model, no budget)
- ``POST /api/v1/marshal/ask``       one question, answered as a server-sent event stream
- ``GET  /api/v1/marshal/settings``  the account's desk setting (``{"desk": bool}``)
- ``PUT  /api/v1/marshal/settings``  turn the desk off or on for the account

Only a dashboard login gets through (:mod:`remembra.marshal.desk.gate`); API
keys, connector grants and the desk's own in-process principal are refused.
``REMEMBRA_MARSHAL_ENABLED`` turns the routes on (404 otherwise) and
``REMEMBRA_MARSHAL_ALLOW_USERS`` limits them to listed accounts.

An ask is refused before its stream starts when the model is offline (no key,
its circuit open: 503), or the ledger refuses it (the account's 40 questions a
day: 429; the platform's day or month dollars spent, or the ledger unreadable:
503). Its reads are GETs as the same user through this app; its answer is
checked before it is sent; its spend is Remembra's, never the user's credits.
Nothing is stored but counts and dollars.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from remembra.auth.middleware import get_client_ip
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.marshal.desk import budget
from remembra.marshal.desk.board import build_board
from remembra.marshal.desk.constants import KEEPALIVE_S, MAX_HISTORY_ANSWER_CHARS, MAX_HISTORY_TURNS, MAX_QUESTION_CHARS
from remembra.marshal.desk.events import KEEPALIVE, error_event, sse
from remembra.marshal.desk.gate import DeskSettingsUser, DeskUser, desk_enabled_for, desk_error, set_desk_enabled
from remembra.marshal.desk.llm import desk_breaker, offline_reason
from remembra.marshal.desk.logs import log
from remembra.marshal.desk.loop import TurnContext, give_back, release_hold, run_turn
from remembra.marshal.diagnosis import canonical_agent_id
from remembra.relay.adapters import REGISTRY
from remembra.security.secrets import redact_secrets
from remembra.security.untrusted import strip_hidden

router = APIRouter(prefix="/marshal", tags=["marshal"])

OFFLINE_TODAY = "Marshal's model is off for today. Rules-only checks still work."
OFFLINE_MESSAGES: dict[str, str] = {
    "no_key": OFFLINE_TODAY,
    "breaker_open": OFFLINE_TODAY,
    "daily_budget": OFFLINE_TODAY,
    "monthly_budget": "Marshal's model is off until next month. Rules-only checks still work.",
    "budget_unavailable": OFFLINE_TODAY,
}
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def daily_limit_message(limit: int) -> str:
    return f"{limit} questions today is the limit. Rules-only checks still work."


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)]


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
    answer: Annotated[str, Field(max_length=MAX_HISTORY_ANSWER_CHARS)]


class AskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=1, max_length=128)

    @field_validator("agent_id")
    @classmethod
    def _registry_agent(cls, value: str) -> str:
        agent = canonical_agent_id(value)
        if agent not in REGISTRY:
            raise ValueError(f"agent_id must be one of {', '.join(REGISTRY)}")
        return agent


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Question
    conv: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$", description="The dashboard's id for this desk session")]
    history: Annotated[list[HistoryTurn], Field(max_length=MAX_HISTORY_TURNS)] = Field(default_factory=list)
    context: AskContext | None = None
    source: Literal["palette", "why_slip", "board", "prompt"] = "prompt"


class DeskSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desk: StrictBool


def clean_input(text: str) -> tuple[str, int]:
    """What the user typed, before the model sees it: hidden characters out, key-shaped strings redacted."""
    redacted = redact_secrets(strip_hidden(text)[0])
    return redacted.text, sum(redacted.counts.values())


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


class DeskStream(StreamingResponse):
    """One ask's event stream. However it ends (the last event, or the client leaving at any point), its
    event source is closed, which stops the turn; and an ask whose turn never began, because the client left
    before the stream started, is given back (nothing was read or spent, and it isn't counted)."""

    def __init__(self, ctx: TurnContext) -> None:
        self.ctx = ctx
        super().__init__(_stream(ctx), media_type="text/event-stream", headers=STREAM_HEADERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                closer = getattr(self.body_iterator, "aclose", None)
                if closer is not None:
                    await closer()
            finally:
                if not self.ctx.turn_began:
                    give_back(self.ctx)


async def _stream(ctx: TurnContext) -> AsyncIterator[bytes]:
    """The turn's events as SSE, a keepalive comment every 10 s without one; the turn stops when the client does."""
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
    task = asyncio.create_task(run_turn(ctx, lambda event, data: queue.put_nowait((event, data))), name="marshal-desk-turn")
    task.add_done_callback(lambda _t: queue.put_nowait(None))
    sent: list[str] = []
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield KEEPALIVE
                continue
            if item is None:
                break
            sent.append(item[0])
            yield sse(*item)
            if item[0] == "done":
                break
        if "done" not in sent:
            # run_turn always ends with answer|error, usage, done; if it crashed, the stream still does.
            problem = task.exception() if task.done() and not task.cancelled() else None
            log.error("marshal_stream_error", error_type=type(problem).__name__ if problem else "Incomplete", status_code=200)
            if "answer" not in sent and "error" not in sent:
                yield sse("error", error_event("internal"))
            if "usage" not in sent:
                yield sse("usage", _empty_usage(ctx))
            yield sse("done", {"ok": "answer" in sent})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _empty_usage(ctx: TurnContext) -> dict[str, Any]:
    return {
        "model": ctx.settings.marshal_model,
        "reads": 0,
        "model_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "usd": 0.0,
        "billed_to_credits": False,
        "asks_today": ctx.reservation.user_asks,
        "asks_limit": int(ctx.settings.marshal_user_daily_asks),
        "input_redactions": ctx.input_redactions,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/board", summary="What needs a look, by rules only (no model)")
@limiter.limit("30/minute")
async def board(request: Request, user: DeskUser) -> dict[str, Any]:
    """Up to three calls about agents whose baton dropped (the "why?" verdicts),
    the status line, suggested questions, the model's state and today's asks.
    Built by rules from your keys, trail and inbox: no model is called, no
    budget is spent, nothing is written."""
    return await build_board(request.app.state, user, datetime.now(UTC), get_settings())


@router.post(
    "/ask",
    summary="Ask Marshal (server-sent events)",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "read* (answer | error) usage done"}},
)
@limiter.limit("20/minute")
async def ask(request: Request, body: AskRequest, user: DeskUser) -> StreamingResponse:
    """One question about your relay, answered from reads of your own records.

    The stream is ``read`` events (at most 4: what was read, with a one-line
    summary), then one ``answer`` (checked: every figure and quote from a read,
    only template commands; otherwise the rules' own summary) or ``error``,
    then ``usage`` (the model's cost, never billed to your credits) and
    ``done``. ``history`` is the last answered turns, sent by the dashboard;
    nothing is stored. A key-shaped string in the question is removed before
    the model sees it."""
    settings = get_settings()
    reason = offline_reason(settings)
    if reason is not None:
        log.info("marshal_budget_refused", reason=reason)
        extra: dict[str, Any] = {"reason": reason}
        if reason == "breaker_open":
            retry = desk_breaker(settings).retry_after()
            extra["retry_after_seconds"] = round(retry) if retry else None
        raise desk_error(status.HTTP_503_SERVICE_UNAVAILABLE, "marshal_offline", OFFLINE_MESSAGES[reason], **extra)
    question, redactions = clean_input(body.question)
    history: list[tuple[str, str]] = []
    for turn in body.history:
        asked, _n_q = clean_input(turn.question)
        answered, _n_a = clean_input(turn.answer)
        history.append((asked, answered))
    client_ip = get_client_ip(request)
    now = datetime.now(UTC)
    try:
        reservation = await budget.reserve(request.app.state.db, user.user_id, now, settings)
    except budget.DailyAskLimit as e:
        log.info("marshal_budget_refused", reason="daily_asks")
        raise desk_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "marshal_daily_limit",
            daily_limit_message(e.limit),
            limit=e.limit,
            used=e.used,
            resets_at=budget.iso_z(e.resets_at),
        ) from None
    except (budget.DailyBudgetSpent, budget.MonthlyBudgetSpent) as e:
        log.info("marshal_budget_refused", reason=e.reason)
        raise desk_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "marshal_offline",
            OFFLINE_MESSAGES[e.reason],
            reason=e.reason,
            resets_at=budget.iso_z(e.resets_at),
        ) from None
    except budget.BudgetRefused:
        log.info("marshal_budget_refused", reason="budget_unavailable")
        message = OFFLINE_MESSAGES["budget_unavailable"]
        raise desk_error(status.HTTP_503_SERVICE_UNAVAILABLE, "marshal_offline", message, reason="budget_unavailable") from None
    try:
        ctx = TurnContext(
            app=request.app,
            settings=settings,
            user=user,
            conv=body.conv,
            question=question,
            history=history,
            context_agent=body.context.agent_id if body.context else None,
            source=body.source,
            reservation=reservation,
            client_ip=client_ip,
            now=now,
            input_redactions=redactions,
            transport=getattr(request.app.state, "marshal_llm_transport", None),
        )
        return DeskStream(ctx)
    except BaseException:
        release_hold(request.app.state.db, reservation, user.user_id)
        raise


@router.get("/settings", summary="The account's Marshal desk setting")
@limiter.limit("30/minute")
async def get_desk_settings(request: Request, user: DeskSettingsUser) -> dict[str, bool]:
    """``{"desk": true}`` unless the account turned the desk off (then board and ask answer 403)."""
    return {"desk": await desk_enabled_for(request.app.state.db, user.user_id)}


@router.put("/settings", summary="Turn the Marshal desk off or on for the account")
@limiter.limit("10/minute")
async def put_desk_settings(request: Request, body: DeskSettingsBody, user: DeskSettingsUser) -> dict[str, bool]:
    """``{"desk": false}`` turns the desk off (the dashboard stops showing it); rules-only checks keep working."""
    await set_desk_enabled(request.app.state.db, user.user_id, body.desk)
    return {"desk": body.desk}
