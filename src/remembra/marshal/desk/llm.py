"""The desk's model client: its own key, its own circuit breaker, and a strict per-call spend bound.

* The key is ``marshal_openai_api_key``, falling back to ``openai_api_key``;
  with neither, the desk is offline (``no_key``).
* Every request goes through ``BreakerTransport`` on the ``llm_marshal``
  breaker (5 failures, or one quota error, open it), never the enrichment
  ``llm`` breaker, so the desk and memory enrichment can't take each other down.
* :func:`desk_chat` works out a strict upper bound of a call's cost before
  making it (every byte of the request as a prompt token, the whole
  ``max_tokens`` as output, all at the uncached price) and holds it against the
  ask's $0.02 :class:`~remembra.core.ai_spend.SpendJob`; a call whose bound
  doesn't fit is never made (:class:`~remembra.core.ai_spend.SpendBudgetExceeded`).
  The real cost is recorded from the response's usage block, or the bound
  itself when a response has none.
* A call whose request reached the provider and brought no usage back (the
  client left, the ask or the call timed out, the reply didn't parse) is
  recorded at its bound too: the provider may still bill it, and the ask
  counts. :class:`SentMarker`, inside the breaker, tells such a call from one
  that never left (the circuit was open, the connection was refused); an
  error status from the provider is not billed, so it records nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import httpx
import openai

from remembra.cloud.model_prices import usd_for
from remembra.core import ai_spend
from remembra.core.circuit_breaker import CircuitBreaker, get_breaker
from remembra.core.llm_guard import make_llm_client
from remembra.marshal.desk.constants import MAX_TOKENS, TEMPERATURE
from remembra.marshal.desk.prompt import ANSWER_FORMAT

BREAKER_NAME = "llm_marshal"
# Per-message overhead of the chat format, and the reply's priming, in tokens (generous on purpose).
TOKENS_PER_MESSAGE = 8
TOKENS_PER_CALL = 64


def desk_breaker(settings: Any = None) -> CircuitBreaker:
    from remembra.config import get_settings

    cfg = settings or get_settings()
    return get_breaker(
        BREAKER_NAME,
        failure_threshold=5,
        reset_timeout=30.0,
        quota_reset_timeout=float(getattr(cfg, "provider_quota_reset_seconds", 900.0)),
    )


def desk_key(settings: Any) -> str | None:
    return settings.marshal_openai_api_key or settings.openai_api_key or None


def offline_reason(settings: Any) -> str | None:
    """``no_key`` or ``breaker_open`` when the model can't be asked now, else None."""
    if not desk_key(settings):
        return "no_key"
    if not desk_breaker(settings).allows_requests():
        return "breaker_open"
    return None


class _Mark:
    """Whether the current call's request was handed to the network."""

    __slots__ = ("sent",)

    def __init__(self) -> None:
        self.sent = False


_mark: ContextVar[_Mark | None] = ContextVar("remembra_marshal_desk_sent", default=None)
# The request never left the machine: no connection was made, or none came free from the pool.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class SentMarker(httpx.AsyncBaseTransport):
    """Marks :func:`desk_chat`'s current call as sent once its request is handed to the network.

    It sits inside ``BreakerTransport``, so a request the open circuit refuses
    never reaches it; a connection that fails takes the mark back.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        mark = _mark.get()
        if mark is not None:
            mark.sent = True
        try:
            return await self.inner.handle_async_request(request)
        except _NOT_SENT:
            if mark is not None:
                mark.sent = False
            raise

    async def aclose(self) -> None:
        await self.inner.aclose()


def client_for(settings: Any, transport: httpx.AsyncBaseTransport | None = None) -> Any:
    """An ``AsyncOpenAI`` client on the desk's key, base URL and breaker. No SDK retries: a retry is unbudgeted spend."""
    return make_llm_client(
        desk_key(settings),
        base_url=settings.marshal_openai_base_url,
        max_retries=0,
        inner_transport=SentMarker(transport or httpx.AsyncHTTPTransport()),
        breaker=desk_breaker(settings),
    )


def _content_bytes(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


def prompt_tokens_upper(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    """A bound no tokenizer can exceed: a byte-level BPE token covers at least one byte."""
    total = 0
    for message in messages:
        total += _content_bytes(message.get("content"))
        if message.get("tool_calls"):
            total += _content_bytes(message["tool_calls"])
    total += _content_bytes(tools or [])
    total += _content_bytes(ANSWER_FORMAT)
    return total + TOKENS_PER_MESSAGE * len(messages) + TOKENS_PER_CALL


def upper_bound_usd(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> float:
    usage = {"prompt_tokens": prompt_tokens_upper(messages, tools), "completion_tokens": MAX_TOKENS}
    return usd_for(usage, model)


async def desk_chat(
    client: Any,
    job: ai_spend.SpendJob,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str,
    on_send: Callable[[], None] | None = None,
) -> Any:
    """One metered call under the ask's budget (raises ``SpendBudgetExceeded`` before the call when it can't fit).

    ``on_send`` runs once the call is admitted, just before it leaves (the turn counts it as a model call).
    A call that raises, or is cancelled, once its request was sent is recorded at its bound before the
    exception goes on, unless the provider answered with an error status.
    """
    bound = upper_bound_usd(model, messages, tools)
    job.hold(bound)
    mark = _Mark()
    token = _mark.set(mark)
    try:
        if on_send is not None:
            on_send()
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=ANSWER_FORMAT,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
    except BaseException as e:
        job.unhold(bound)
        if mark.sent and not isinstance(e, openai.APIStatusError):
            ai_spend.record_unanswered(bound)
        raise
    finally:
        _mark.reset(token)
    job.unhold(bound)
    if getattr(response, "usage", None) is None:
        # An unmetered response is counted at its bound: the ledger never under-counts.
        await ai_spend.charge_flat(bound, held=False)
    else:
        ai_spend.record_llm_usage(response, model)
    return response
