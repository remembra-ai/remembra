"""One desk question: reads, the model, the validator, and the stream's events (plan 4.1).

:func:`run_turn` builds the messages (system prompt, the client's history,
the question), runs the context pre-read when the question came from a "why?"
slip, then calls the model at most ``MAX_MODEL_CALLS`` times: each call may
ask for reads (at most ``MAX_TOOL_CALLS`` per question; later ones get
``not_run``), and the last one, or any after the fourth read, is made with
``tool_choice="none"``. The first reply with content is validated and becomes
the ``answer``. Every path ends with exactly one ``answer`` or ``error``, then
``usage`` and ``done``.

The whole loop runs inside ``ai_spend.activate`` of a $0.02 ``SpendJob`` whose
settle callback releases the ask's hold in the ledger with the real dollars.
The job carries no credit reservation: this spend is Remembra's, never the
user's smart credits.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import openai

from remembra.auth.middleware import AuthenticatedUser
from remembra.core import ai_spend
from remembra.core.ai_spend import SpendBudgetExceeded, SpendJob
from remembra.marshal import commands
from remembra.marshal.desk import budget
from remembra.marshal.desk.client import DeskClient
from remembra.marshal.desk.constants import ASK_BUDGET_USD, ASK_TIMEOUT_S, MAX_MODEL_CALLS, MAX_TOOL_CALLS
from remembra.marshal.desk.events import ErrorCode, UsageEvent, error_event
from remembra.marshal.desk.llm import client_for, desk_chat
from remembra.marshal.desk.logs import ask_log, log
from remembra.marshal.desk.principal import conv_hash, read_principal
from remembra.marshal.desk.prompt import command_lines, context_line, fixed_facts, system_prompt
from remembra.marshal.desk.tools import TOOL_SPECS, TOOLS, ReadResult, ToolContext, run_tool
from remembra.marshal.desk.validate import Answer, fallback_answer, validate_answer

Emit = Callable[[str, Any], None]
NOT_RUN_LIMIT = json.dumps({"status": "not_run", "reason": f"{MAX_TOOL_CALLS} reads per question"})
NOT_RUN_UNKNOWN = json.dumps({"status": "not_run", "reason": "no such tool"})
SETTLE_WAIT_S = 5.0


@dataclass
class TurnContext:
    """Everything one question needs; the question and history are already cleaned."""

    app: Any
    settings: Any
    user: AuthenticatedUser
    conv: str
    question: str
    history: list[tuple[str, str]]
    context_agent: str | None
    source: str
    reservation: budget.Reservation
    client_ip: str
    now: datetime
    input_redactions: int = 0
    transport: httpx.AsyncBaseTransport | None = None
    # Set by run_turn before anything else: from then on the turn's own spend job settles the hold.
    turn_began: bool = False


@dataclass
class TurnOutcome:
    outcome: str  # answer | fallback | error
    fallback_reason: str | None
    reads: list[ReadResult]
    model_calls: int
    usage: UsageEvent


@dataclass
class _State:
    reads: list[ReadResult] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0

    def count_call(self) -> None:
        self.model_calls += 1

    def tally(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        self.cached_tokens += int(getattr(details, "cached_tokens", 0) or 0)


def server_urls(settings: Any) -> list[str | None]:
    return [settings.public_url, commands.CLOUD_URL]


def _tool_call_dict(call: Any) -> dict[str, Any]:
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}


def _messages(ctx: TurnContext) -> list[dict[str, Any]]:
    url = ctx.settings.public_url or commands.CLOUD_URL
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(url)}]
    for question, answer in ctx.history:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    question = ctx.question
    if ctx.context_agent:
        question = f"{question}\n\n{context_line(ctx.context_agent)}"
    messages.append({"role": "user", "content": question})
    return messages


async def _read(name: str, args: Any, tool_ctx: ToolContext, state: _State, emit: Emit) -> ReadResult:
    read = await run_tool(name, args, tool_ctx, f"r{len(state.reads) + 1}")
    state.reads.append(read)
    emit("read", read.as_event())
    return read


async def _loop(ctx: TurnContext, job: SpendJob, client: Any, state: _State, emit: Emit) -> Answer:
    settings = ctx.settings
    urls = server_urls(settings)
    facts = [*command_lines(ctx.settings.public_url or commands.CLOUD_URL), *fixed_facts()]
    tool_ctx = ToolContext(DeskClient(ctx.app, read_principal(ctx.user, ctx.conv), ctx.client_ip), ctx.now)
    messages = _messages(ctx)
    if ctx.context_agent:
        # The "why?" slip's agent: its verdict is read first (r1) and counts toward the reads.
        call_id = "call_context_r1"
        arguments = json.dumps({"agent_id": ctx.context_agent})
        read = await _read("diagnose_agent", arguments, tool_ctx, state, emit)
        pre_read = {"id": call_id, "type": "function", "function": {"name": "diagnose_agent", "arguments": arguments}}
        messages.append({"role": "assistant", "content": None, "tool_calls": [pre_read]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": read.message})
    for call in range(1, MAX_MODEL_CALLS + 1):
        tool_choice = "none" if len(state.reads) >= MAX_TOOL_CALLS or call == MAX_MODEL_CALLS else "auto"
        response = await desk_chat(
            client,
            job,
            model=settings.marshal_model,
            messages=messages,
            tools=TOOL_SPECS,
            tool_choice=tool_choice,
            on_send=state.count_call,
        )
        state.tally(response)
        message = response.choices[0].message if response.choices else None
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if tool_calls:
            if tool_choice == "none":
                return fallback_answer("tool_choice_ignored", state.reads, server_urls=urls)
            calls = [_tool_call_dict(c) for c in tool_calls]
            messages.append({"role": "assistant", "content": getattr(message, "content", None), "tool_calls": calls})
            for tool_call in tool_calls:
                name = getattr(getattr(tool_call, "function", None), "name", None)
                if name not in TOOLS:
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": NOT_RUN_UNKNOWN})
                elif len(state.reads) >= MAX_TOOL_CALLS:
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": NOT_RUN_LIMIT})
                else:
                    read = await _read(name, tool_call.function.arguments, tool_ctx, state, emit)
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": read.message})
            continue
        return validate_answer(getattr(message, "content", None), state.reads, server_urls=urls, facts=facts)
    return fallback_answer("tool_choice_ignored", state.reads, server_urls=urls)  # pragma: no cover - the last call can't ask


def _spend_job(ctx: TurnContext) -> SpendJob:
    """The ask's $0.02 job. Settling it releases the hold with the real dollars; the ask counts once a call reached
    the provider (it completed, or was recorded at its bound, see :func:`~remembra.marshal.desk.llm.desk_chat`)."""
    db = ctx.app.state.db

    async def settle(usd: float, _enriched: bool) -> None:
        try:
            await budget.settle(db, ctx.reservation, usd, counted=job.llm_calls > 0)
        except Exception as e:  # the hold stays; the next reserve expires it at the full reserve
            log.error("marshal_settle_failed", error_type=type(e).__name__)

    job = SpendJob(user_id=ctx.user.user_id, label="marshal", budget_usd=ASK_BUDGET_USD, settle=settle)
    return job


def release_hold(db: Any, reservation: budget.Reservation, user_id: str) -> None:
    """Release an unspent hold even if construction of the turn itself failed."""

    async def settle(usd: float, _enriched: bool) -> None:
        try:
            await budget.settle(db, reservation, usd, counted=False)
        except Exception as e:
            log.error("marshal_settle_failed", error_type=type(e).__name__)

    SpendJob(user_id=user_id, label="marshal", budget_usd=ASK_BUDGET_USD, settle=settle).release()


def give_back(ctx: TurnContext) -> None:
    """Release the hold of an ask whose turn never began (its client left first): nothing read, nothing spent.

    The ask isn't counted. It settles as a turn's job does (tracked, and awaited by ``drain_settles`` at shutdown).
    """
    release_hold(ctx.app.state.db, ctx.reservation, ctx.user.user_id)


async def run_turn(ctx: TurnContext, emit: Emit) -> TurnOutcome:
    """Run one question and emit its events; always ends with answer|error, usage, done."""
    ctx.turn_began = True
    settings = ctx.settings
    state = _State()
    started = time.monotonic()
    job = _spend_job(ctx)
    client: Any = None
    answer: Answer | None = None
    error: ErrorCode = "internal"
    try:
        try:
            with ai_spend.activate(job):
                client = client_for(settings, ctx.transport)
                async with asyncio.timeout(ASK_TIMEOUT_S):
                    answer = await _loop(ctx, job, client, state, emit)
        except SpendBudgetExceeded:
            answer = fallback_answer("model_budget", state.reads, server_urls=server_urls(settings))
        except TimeoutError:
            error = "timeout"
        except (openai.APIError, httpx.HTTPError):
            error = "model_unavailable"
        except Exception as e:
            log.error("marshal_turn_failed", error_type=type(e).__name__)
            error = "internal"
    except asyncio.CancelledError:
        # The client went away: nothing more is sent; the job settles with what was spent, a call in flight at its bound.
        _log(ctx, state, job, "error", None, started)
        raise
    finally:
        if client is not None:
            await client.close()
    if answer is not None:
        emit("answer", answer.as_event())
    else:
        emit("error", error_event(error))
    try:
        await job.wait_settled(timeout=SETTLE_WAIT_S)
    except TimeoutError:
        log.warning("marshal_settle_slow")
    usage = _usage(ctx, state, job)
    emit("usage", usage)
    emit("done", {"ok": answer is not None})
    outcome = "error" if answer is None else ("fallback" if answer.fallback else "answer")
    reason = answer.fallback_reason if answer is not None else None
    _log(ctx, state, job, outcome, reason, started)
    return TurnOutcome(outcome, reason, state.reads, state.model_calls, usage)


def _usage(ctx: TurnContext, state: _State, job: SpendJob) -> UsageEvent:
    counted = job.llm_calls > 0
    return {
        "model": ctx.settings.marshal_model,
        "reads": len(state.reads),
        "model_calls": state.model_calls,
        "input_tokens": state.input_tokens,
        "cached_tokens": state.cached_tokens,
        "output_tokens": state.output_tokens,
        "usd": round(job.usd, 6),
        "billed_to_credits": False,
        # The ask counts once a model call reached the provider; otherwise it was given back at settlement.
        "asks_today": ctx.reservation.user_asks - (0 if counted else 1),
        "asks_limit": int(ctx.settings.marshal_user_daily_asks),
        "input_redactions": ctx.input_redactions,
    }


def _log(ctx: TurnContext, state: _State, job: SpendJob, outcome: str, reason: str | None, started: float) -> None:
    ask_log(
        user_id=ctx.user.user_id,
        conv_hash=conv_hash(ctx.user.user_id, ctx.conv),
        turn=len(ctx.history) + 1,
        source=ctx.source,
        model=ctx.settings.marshal_model,
        model_calls=state.model_calls,
        tools=[read.tool for read in state.reads],
        reads_failed=sum(1 for read in state.reads if not read.ok),
        input_tokens=state.input_tokens,
        cached_tokens=state.cached_tokens,
        output_tokens=state.output_tokens,
        usd=round(job.usd, 6),
        outcome=outcome,
        fallback_reason=reason,
        duration_ms=int((time.monotonic() - started) * 1000),
        input_redactions=ctx.input_redactions,
    )
