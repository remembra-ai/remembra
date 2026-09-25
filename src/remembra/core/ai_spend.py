"""Per-request AI spend tracking and budgeting (smart-credit settlement).

A :class:`SpendJob` is opened by the API layer when a write is granted
enrichment (a credit reservation was taken). It is published through a
context variable, so every paid AI call made while serving that write — in the
request itself or in background tasks spawned from it (asyncio copies the
context into new tasks) — is checked against the job's budget BEFORE it is
made and reports its real cost afterwards.

* **Budget.** The job carries the dollars its reservation holds
  (``reserved credits x CREDIT_USD``). :func:`metered_chat` (every OpenAI chat
  completion) and :func:`charge_flat` (TypeSafe calls) estimate the call first
  and raise :class:`SpendBudgetExceeded` when the job's spent + in-flight
  dollars plus the estimate would pass the budget. Callers already treat a
  failed model call as "fall back to the non-LLM default" (verbatim sentences,
  ADD, no entities), so the rest of the write degrades to atomic, and queued
  droppable enrichment of an exhausted job is skipped.
* **Holders.** The job counts the request plus every background task spawned
  through the enrichment queue or ``extraction.background.spawn``. When the
  last holder finishes, the job settles exactly once: the settle callback
  receives the actual dollars spent, so the reservation can be reconciled and
  the unused part refunded.
* **Liveness.** Jobs with a reservation register themselves in a process-wide
  weak registry until they settle, so stale-reservation expiry never releases
  a hold whose work is still running in this process.
* **Unattributed spend.** Paid AI calls outside a write (Jev query-intent on a
  recall) have no job. An :class:`AttributionPolicy` (the cloud usage meter)
  decides whether such a call may run for a user and records its dollars in
  the monthly AI-spend totals that feed the free-tier breaker.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

SettleCallback = Callable[[float, bool], Awaitable[Any]]

_current: ContextVar[SpendJob | None] = ContextVar("remembra_spend_job", default=None)
# User an unattributed AI call (no spend job) is made for, e.g. a recall's Jev call.
_attributed_user: ContextVar[str | None] = ContextVar("remembra_ai_user", default=None)

# Process-wide totals (observability; not used for billing).
_totals: dict[str, float] = {"usd": 0.0, "calls": 0.0, "untracked_usd": 0.0, "blocked_calls": 0.0}

# Reservation id -> job, for jobs whose work is still running in this process.
_live_reservations: weakref.WeakValueDictionary[str, SpendJob] = weakref.WeakValueDictionary()
# Settle tasks not yet finished (awaited on shutdown so no settle is lost).
_settle_tasks: set[asyncio.Task[Any]] = set()

# Output tokens assumed for a chat call that does not cap them.
DEFAULT_OUTPUT_TOKENS_ESTIMATE = 800
# Characters per token used to estimate prompt size (conservative for English).
CHARS_PER_TOKEN_ESTIMATE = 3


class SpendBudgetExceeded(RuntimeError):
    """The current write's reserved AI budget cannot cover another paid call."""


class SpendJob:
    """AI spend attributed to one metered write (or one sleep-time pass)."""

    def __init__(
        self,
        *,
        user_id: str,
        settle: SettleCallback | None = None,
        concurrency: int | None = None,
        label: str = "write",
        budget_usd: float | None = None,
        reservation_id: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.concurrency = concurrency
        self.label = label
        self.budget_usd = budget_usd
        self.reservation_id = reservation_id
        self.usd = 0.0
        self.inflight_usd = 0.0
        self.llm_calls = 0
        self.blocked_calls = 0
        self.exhausted = False
        self.enriched = True
        self.settled_result: Any = None
        self._settle_cb = settle
        self._holders = 1  # the opener (the request); released by activate()
        self._closed = False
        self._done = asyncio.Event()
        if reservation_id is not None:
            _live_reservations[reservation_id] = self

    # -- spend ----------------------------------------------------------
    def add(self, usd: float) -> None:
        self.usd += max(0.0, usd)
        self.llm_calls += 1

    def remaining_usd(self) -> float | None:
        if self.budget_usd is None:
            return None
        return max(0.0, self.budget_usd - self.usd - self.inflight_usd)

    def hold(self, estimate_usd: float) -> None:
        """Admit one paid call of ``estimate_usd`` or raise :class:`SpendBudgetExceeded`.

        Concurrent calls of the same write see each other's in-flight estimates,
        so parallel batch items cannot all pass the check at once.
        """
        estimate_usd = max(0.0, estimate_usd)
        if self.budget_usd is not None and (
            self.exhausted or self.usd + self.inflight_usd + estimate_usd > self.budget_usd + 1e-12
        ):
            if not self.exhausted:
                log.info(
                    "ai_budget_exhausted",
                    user_id=self.user_id,
                    label=self.label,
                    budget_usd=round(self.budget_usd, 5),
                    spent_usd=round(self.usd, 5),
                )
            self.exhausted = True
            self.blocked_calls += 1
            _totals["blocked_calls"] += 1
            raise SpendBudgetExceeded(f"AI budget of this write is used up (${self.budget_usd:.4f})")
        self.inflight_usd += estimate_usd

    def unhold(self, estimate_usd: float) -> None:
        self.inflight_usd = max(0.0, self.inflight_usd - max(0.0, estimate_usd))

    # -- lifetime -------------------------------------------------------
    @property
    def settled(self) -> bool:
        return self._done.is_set()

    @property
    def alive(self) -> bool:
        """True until the job has settled (its work may still be running)."""
        return not self._done.is_set()

    def acquire(self) -> None:
        """Register one more piece of work (a background task) that must finish first."""
        if self._closed:
            # Work started after settlement: its spend can no longer be billed
            # through this reservation. Should not happen; keep it visible.
            log.warning("spend_job_acquire_after_settle", user_id=self.user_id, label=self.label)
            return
        self._holders += 1

    def release(self) -> None:
        if self._closed:
            return
        self._holders -= 1
        if self._holders <= 0:
            self._closed = True
            self._schedule_settle()

    def _schedule_settle(self) -> None:
        from remembra.core.tasks import get_task_registry

        try:
            task = get_task_registry().spawn(self._settle(), name="credit_settle")
        except RuntimeError:
            # Registry shut down (process stopping). spawn() closed the coroutine
            # it was given, so settle with a fresh one on the running loop; the
            # task is tracked and awaited by drain_settles() before the DB closes.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                log.error("spend_job_settle_lost_no_loop", user_id=self.user_id, label=self.label)
                return
            task = loop.create_task(self._settle(), name="credit_settle")
        _settle_tasks.add(task)
        task.add_done_callback(_settle_tasks.discard)

    async def _settle(self) -> None:
        try:
            if self._settle_cb is not None:
                self.settled_result = await self._settle_cb(self.usd, self.enriched)
        except Exception as e:  # never lose the error; the reservation expires if this failed
            log.error("spend_job_settle_failed", user_id=self.user_id, error=str(e), error_type=type(e).__name__)
        finally:
            self._done.set()
            if self.reservation_id is not None and _live_reservations.get(self.reservation_id) is self:
                del _live_reservations[self.reservation_id]

    async def wait_settled(self, timeout: float = 10.0) -> Any:
        """Wait until the job settled (tests, graceful shutdown). Returns the settle result."""
        await asyncio.wait_for(self._done.wait(), timeout=timeout)
        return self.settled_result


def current_job() -> SpendJob | None:
    return _current.get()


def is_live_reservation(reservation_id: str) -> bool:
    """True while a job holding ``reservation_id`` is still running in this process."""
    job = _live_reservations.get(reservation_id)
    return job is not None and job.alive


def live_reservation_ids() -> set[str]:
    return {rid for rid, job in list(_live_reservations.items()) if job.alive}


def pending_settles() -> int:
    return len(_settle_tasks)


async def drain_settles(timeout: float = 10.0) -> None:
    """Wait (bounded) for every scheduled credit settle — graceful shutdown, before the DB closes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while _settle_tasks:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            log.error("credit_settles_not_drained", pending=len(_settle_tasks))
            return
        await asyncio.wait(set(_settle_tasks), timeout=remaining)


@contextmanager
def activate(job: SpendJob | None) -> Iterator[SpendJob | None]:
    """Publish ``job`` for the duration of the block; release the opener's hold on exit.

    If the block raises, the job is marked not-enriched: only the dollars
    actually spent are charged, not the chunk minimum.
    """
    if job is None:
        yield None
        return
    token = _current.set(job)
    try:
        yield job
    except BaseException:
        job.enriched = False
        raise
    finally:
        _current.reset(token)
        job.release()


# ---------------------------------------------------------------------------
# Metered calls
# ---------------------------------------------------------------------------


def _message_chars(messages: Sequence[Any]) -> int:
    total = 0
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(part)) for part in content)
    return total


def estimate_chat_usd(model: str, messages: Sequence[Any], max_output_tokens: int | None = None) -> float:
    """Estimate of one chat completion before it is made.

    The prompt is sized conservatively (3 characters per token); the output at
    a typical enrichment reply (``DEFAULT_OUTPUT_TOKENS_ESTIMATE``, or the
    call's lower ``max_tokens``), not at the cap: sizing every call at its
    output cap would refuse most calls on pricier models. A call that runs
    past its estimate is still bounded, because settlement never charges more
    than the reservation.
    """
    from remembra.cloud.model_prices import usd_for

    prompt_tokens = _message_chars(messages) // CHARS_PER_TOKEN_ESTIMATE + 16
    completion = DEFAULT_OUTPUT_TOKENS_ESTIMATE
    if isinstance(max_output_tokens, int) and 0 < max_output_tokens < completion:
        completion = max_output_tokens
    return usd_for({"prompt_tokens": prompt_tokens, "completion_tokens": completion}, model)


async def metered_chat(client: Any, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    """``client.chat.completions.create`` under the current job's AI budget.

    Raises :class:`SpendBudgetExceeded` (before any network call) when the
    write's reservation cannot cover the call; records the real cost after.
    """
    estimate = estimate_chat_usd(model, messages, kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"))
    job = _current.get()
    if job is not None:
        job.hold(estimate)
    try:
        response = await client.chat.completions.create(model=model, messages=messages, **kwargs)
    finally:
        if job is not None:
            job.unhold(estimate)
    record_llm_usage(response, model)
    return response


def record_llm_usage(response: Any, model: str) -> float:
    """Attribute the cost of one chat completion to the current job. Returns the dollars."""
    from remembra.cloud.model_prices import usd_for

    usd = usd_for(getattr(response, "usage", None), model)
    _record(usd)
    return usd


def _record(usd: float) -> None:
    _totals["usd"] += usd
    _totals["calls"] += 1
    job = _current.get()
    if job is not None:
        job.add(usd)
    elif usd > 0:
        _totals["untracked_usd"] += usd


def hold_flat(usd: float) -> None:
    """Admit a flat-priced paid call (TypeSafe) under the current job's budget, or raise."""
    job = _current.get()
    if job is not None:
        job.hold(usd)


async def charge_flat(usd: float, *, held: bool = True) -> None:
    """Record a flat-priced paid call that was made (after :func:`hold_flat`).

    With a job the dollars settle into the write's credits. Without one, the
    :class:`AttributionPolicy` records them for the attributed user (monthly
    AI-spend totals feeding the free-tier breaker).
    """
    job = _current.get()
    if job is not None:
        if held:
            job.unhold(usd)
        _record(usd)
        return
    _record(usd)
    user_id = _attributed_user.get()
    policy = _policy
    if user_id and policy is not None and usd > 0:
        try:
            await policy.record_unattributed_ai(user_id, usd)
        except Exception as e:  # observability must never break a recall
            log.warning("unattributed_ai_spend_record_failed", error_type=type(e).__name__)


def release_flat(usd: float) -> None:
    """Undo :func:`hold_flat` for a call that failed before it cost anything."""
    job = _current.get()
    if job is not None:
        job.unhold(usd)


# ---------------------------------------------------------------------------
# Unattributed AI calls (no spend job): policy hook
# ---------------------------------------------------------------------------


class AttributionPolicy(Protocol):
    async def allow_unattributed_ai(self, user_id: str) -> bool: ...

    async def record_unattributed_ai(self, user_id: str, usd: float) -> None: ...


_policy: AttributionPolicy | None = None


def set_attribution_policy(policy: AttributionPolicy | None) -> None:
    global _policy
    _policy = policy


async def allow_unattributed_ai(user_id: str | None) -> bool:
    """May a paid AI call outside any write run for ``user_id``? True without a policy (self-hosted)."""
    if _current.get() is not None:
        return True  # inside a metered write: the job budget decides
    policy = _policy
    if policy is None or not user_id:
        return True
    try:
        return bool(await policy.allow_unattributed_ai(user_id))
    except Exception as e:  # fail closed on the optional AI call, never on the request
        log.warning("unattributed_ai_policy_failed", error_type=type(e).__name__)
        return False


@contextmanager
def attribute_to(user_id: str | None) -> Iterator[None]:
    """Attribute unattributed AI calls inside the block to ``user_id``."""
    token = _attributed_user.set(user_id)
    try:
        yield
    finally:
        _attributed_user.reset(token)


def totals() -> dict[str, float]:
    """Process-wide AI spend observed since start (for /metrics-style views)."""
    return dict(_totals)
