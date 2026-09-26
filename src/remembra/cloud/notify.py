"""Account notifications sent by email from request handlers and billing webhooks.

Each helper finds the account's address, renders the right template and sends
it in the background (the app's task registry), so a slow or failing mail
provider never delays or fails the request that triggered it. With no email
provider configured every helper is a no-op.

- :func:`notify_welcome` after social sign-in creates an account.
- :func:`notify_key_created` after ``POST /api/v1/keys``: a security notice
  (name, access, projects, agent, time), never the key.
- :func:`notify_billing_change` after a Paddle event changed the account's
  plan: "plan changed" (a new plan, interval or seat count; renewals of the
  same plan send nothing) or "subscription ended" (a cancel to Free).
- :func:`notify_payment_failed` on ``subscription.past_due``, once per
  subscription per day even if Paddle redelivers the event.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from remembra.cloud.email import EmailResult, EmailService, email_service_or_none
from remembra.cloud.plans import BillingInterval, PlanTier
from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

PAYMENT_FAILED_DEDUPE_SECONDS = 24 * 3600
_payment_failed_sent: dict[str, float] = {}

# Swapped in tests; production resolves the Resend-backed service.
service_factory: Callable[[], EmailService | None] = email_service_or_none


async def account_email(state: Any, user_id: str) -> str | None:
    """The address of a dashboard user, else the address on the tenant row (API-signup accounts)."""
    db = getattr(state, "db", None)
    if db is not None:
        user = await db.get_user_by_id(user_id)
        if user and user.get("email"):
            return str(user["email"])
    meter = getattr(state, "usage_meter", None)
    if meter is not None:
        tenant = await meter.get_tenant(user_id)
        email = str((tenant or {}).get("email") or "").strip()
        if "@" in email:
            return email
    return None


def _spawn(state: Any, coro: Awaitable[Any], name: str) -> None:
    tasks = getattr(state, "tasks", None)
    try:
        if tasks is not None:
            tasks.spawn(coro, name=name)
        else:
            asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
    except Exception as e:
        close = getattr(coro, "close", None)
        if close is not None:
            close()  # never scheduled (e.g. the registry is shutting down)
        log.warning("notification_not_scheduled", notification=name, error_type=type(e).__name__)


async def _send(template: str, state: Any, user_id: str, send: Callable[[EmailService, str], Awaitable[EmailResult]]) -> None:
    try:
        service = service_factory()
        if service is None:
            return
        to = await account_email(state, user_id)
        if not to:
            log.info("notification_no_address", notification=template, user_id=user_id)
            return
        result = await send(service, to)
        if not result.success:
            log.warning("notification_failed", notification=template, user_id=user_id)
    except Exception as e:  # a notification never fails the work that triggered it
        log.warning("notification_error", notification=template, user_id=user_id, error_type=type(e).__name__)


def notify_welcome(state: Any, user_id: str) -> None:
    """Welcome for an account created by social sign-in (its address is already verified)."""

    async def send(service: EmailService, to: str) -> EmailResult:
        return await service.send_welcome_email(to)

    _spawn(state, _send("welcome", state, user_id, send), "email:welcome")


def notify_key_created(
    state: Any,
    user_id: str,
    *,
    key_name: str | None,
    role: str,
    project_ids: list[str] | None,
    agent_id: str | None,
) -> None:
    created_at = utcnow()

    async def send(service: EmailService, to: str) -> EmailResult:
        return await service.send_key_created_email(
            to, key_name=key_name, role=role, project_ids=project_ids, agent_id=agent_id, created_at=created_at
        )

    _spawn(state, _send("key_created", state, user_id, send), "email:key_created")


def _plan_state(tenant: dict[str, Any] | None) -> tuple[PlanTier, BillingInterval | None, int | None]:
    tenant = tenant or {}
    try:
        tier = PlanTier(str(tenant.get("plan") or PlanTier.FREE.value))
    except ValueError:
        tier = PlanTier.FREE
    raw_interval = tenant.get("billing_interval")
    interval = (
        BillingInterval(raw_interval) if raw_interval in (BillingInterval.MONTH.value, BillingInterval.YEAR.value) else None
    )
    seats = int(tenant["seats"]) if tenant.get("seats") else None
    return tier, interval, seats


def _plan_changed(
    old: tuple[PlanTier, BillingInterval | None, int | None], new: tuple[PlanTier, BillingInterval | None, int | None]
) -> bool:
    """A different plan, or a different known interval / seat count.

    Rows from before intervals and seats were recorded (legacy subscribers) hold
    NULL there; the first renewal filling them in is not a change to announce.
    """
    (old_tier, old_interval, old_seats), (new_tier, new_interval, new_seats) = old, new
    if old_tier != new_tier:
        return True
    if old_interval is not None and new_interval != old_interval:
        return True
    return old_seats is not None and new_seats != old_seats


def notify_billing_change(state: Any, user_id: str, before: dict[str, Any] | None, *, cancelled: bool = False) -> None:
    """Email the account when a billing event changed its plan, interval or seats.

    ``before`` is the tenant row as it was before the event was applied. A
    renewal (same plan, interval and seats) sends nothing.
    """

    async def run() -> None:
        meter = getattr(state, "usage_meter", None)
        if meter is None:
            return
        old_tier, old_interval, old_seats = _plan_state(before)
        after = await meter.get_tenant(user_id)
        new_tier, new_interval, new_seats = _plan_state(after)
        if not _plan_changed((old_tier, old_interval, old_seats), (new_tier, new_interval, new_seats)):
            return
        account = await meter.get_account(user_id)
        if cancelled and new_tier == PlanTier.FREE and old_tier != PlanTier.FREE:

            async def send_cancel(service: EmailService, to: str) -> EmailResult:
                return await service.send_subscription_cancelled_email(to, old_tier=old_tier, memory_cap=account.memory_cap)

            await _send("subscription_cancelled", state, user_id, send_cancel)
            return

        async def send_change(service: EmailService, to: str) -> EmailResult:
            return await service.send_plan_changed_email(
                to,
                old_tier=old_tier,
                new_tier=new_tier,
                interval=new_interval,
                seats=new_seats,
                founding=bool((after or {}).get("founding")),
                memory_cap=account.memory_cap,
            )

        await _send("plan_changed", state, user_id, send_change)

    _spawn(state, run(), "email:billing_change")


def notify_payment_failed(state: Any, user_id: str, subscription_id: str | None) -> None:
    key = subscription_id or f"user:{user_id}"
    now = time.monotonic()
    for stale in [k for k, sent in _payment_failed_sent.items() if now - sent > PAYMENT_FAILED_DEDUPE_SECONDS]:
        _payment_failed_sent.pop(stale, None)
    if key in _payment_failed_sent:
        log.info("payment_failed_email_deduplicated", user_id=user_id)
        return
    _payment_failed_sent[key] = now

    async def run() -> None:
        meter = getattr(state, "usage_meter", None)
        if meter is None:
            return
        tenant = await meter.get_tenant(user_id)
        tier, interval, seats = _plan_state(tenant)
        if tier == PlanTier.FREE:
            return

        async def send(service: EmailService, to: str) -> EmailResult:
            return await service.send_payment_failed_email(
                to, tier=tier, interval=interval, seats=seats, founding=bool((tenant or {}).get("founding"))
            )

        await _send("payment_failed", state, user_id, send)

    _spawn(state, run(), "email:payment_failed")
