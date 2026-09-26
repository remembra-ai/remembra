"""Self-serve account deletion (R-11): confirm, stop billing, then schedule erasure.

Order matters. Billing is cancelled in Paddle FIRST: if Paddle cannot confirm
the cancel, nothing is deleted, the owner is alerted and the user is told to
retry (or, when Paddle refuses outright, that support will sort it out), so an
account can never be gone while its subscription keeps charging. Only this
account's subscriptions are cancelled: one Paddle customer (one payer email)
can pay for several Remembra accounts. A subscription Paddle does not know
(404) cannot bill through Paddle; it counts as done and the owner is alerted. Then the account is
deactivated (sessions and API keys stop working at once) and stamped
``deleted_at``; :mod:`remembra.account.erasure` removes everything it owns once
the grace period has passed.

Password accounts confirm with their password; accounts created through Google
or GitHub (which have no password anyone knows) confirm with a six-digit code
emailed to the account address.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from remembra.cloud.plans import PlanTier

log = structlog.get_logger(__name__)

DELETION_CODE_TTL = timedelta(minutes=15)
DELETION_CODE_MAX_ATTEMPTS = 5


SUPPORT_EMAIL = "support@remembra.dev"


class BillingCancelError(RuntimeError):
    """The subscription could not be confirmed cancelled; the account must not be deleted.

    ``transient``: Paddle could not be reached or answered 429/5xx, so a retry
    in a few minutes may work. Otherwise Paddle refused and a person must look.
    """

    def __init__(self, message: str, *, transient: bool, subscription_id: str | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.subscription_id = subscription_id


@dataclass
class BillingCancelOutcome:
    """What deleting the account did to billing."""

    cancelled: list[str] = field(default_factory=list)
    # Recorded ids Paddle does not know (404): nothing bills through Paddle under them.
    not_found: list[str] = field(default_factory=list)
    # Billable subscriptions of the same Paddle customer that belong to another account (left alone).
    other_accounts: list[str] = field(default_factory=list)


async def notify_owner(app_state: Any, event: str, message: str, details: dict[str, Any]) -> None:
    """Best-effort operator alert (``app.state.alerts``: webhook / email); never raises."""
    alerts = getattr(app_state, "alerts", None)
    if alerts is None:
        return
    tasks = getattr(app_state, "tasks", None)
    coro = alerts.notify(event, message, details)
    try:
        if tasks is not None:
            tasks.spawn(coro, name=f"alert:{event}")
        else:
            await coro
    except Exception as e:
        coro.close()  # never scheduled (e.g. the registry is shutting down)
        log.warning("operator_alert_failed", alert_event=event, error_type=type(e).__name__)


def _code_hash(user_id: str, code: str) -> str:
    return hashlib.sha256(f"account-deletion:v1:{user_id}:{code}".encode()).hexdigest()


async def create_deletion_code(db: Any, user_id: str) -> str:
    """A new single-use six-digit code for ``user_id`` (replaces any earlier one)."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(UTC)
    await db.conn.execute(
        """
        INSERT INTO account_deletion_codes (user_id, code_hash, expires_at, attempts, created_at)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            code_hash = excluded.code_hash, expires_at = excluded.expires_at, attempts = 0,
            created_at = excluded.created_at
        """,
        (user_id, _code_hash(user_id, code), (now + DELETION_CODE_TTL).isoformat(), now.isoformat()),
    )
    await db.conn.commit()
    return code


async def consume_deletion_code(db: Any, user_id: str, code: str) -> bool:
    """True once for the right, unexpired code; five wrong tries burn it."""
    code = (code or "").strip()
    async with db.transaction():
        cursor = await db.conn.execute(
            "SELECT code_hash, expires_at, attempts FROM account_deletion_codes WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        code_hash, expires_at, attempts = str(row[0]), str(row[1]), int(row[2])
        expired = datetime.fromisoformat(expires_at) <= datetime.now(UTC)
        if expired or attempts >= DELETION_CODE_MAX_ATTEMPTS:
            await db.conn.execute("DELETE FROM account_deletion_codes WHERE user_id = ?", (user_id,))
            return False
        if not (code.isdigit() and len(code) == 6 and secrets.compare_digest(code_hash, _code_hash(user_id, code))):
            await db.conn.execute("UPDATE account_deletion_codes SET attempts = attempts + 1 WHERE user_id = ?", (user_id,))
            return False
        await db.conn.execute("DELETE FROM account_deletion_codes WHERE user_id = ?", (user_id,))
    return True


def _paddle_manager() -> Any:
    from remembra.cloud.billing_paddle import PaddleBillingManager
    from remembra.cloud.paddle_config import get_paddle_settings

    paddle = get_paddle_settings()
    return PaddleBillingManager(api_key=paddle.api_key, webhook_secret=paddle.webhook_secret or "", sandbox=paddle.sandbox)


def _is_transient(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        return code == 429 or code >= 500
    return True  # timeouts, connection errors: Paddle was not reached


def _cancel_error(error: httpx.HTTPError, subscription_id: str | None) -> BillingCancelError:
    if _is_transient(error):
        message = (
            "We could not confirm with our billing provider that your subscription is cancelled, so your account "
            "was not deleted. Nothing changed; please try again in a few minutes."
        )
    else:
        message = (
            "Our billing provider did not accept the cancellation of your subscription, so your account was not "
            f"deleted and nothing changed. We have been alerted and will sort it out; you can also email {SUPPORT_EMAIL}."
        )
    return BillingCancelError(message, transient=_is_transient(error), subscription_id=subscription_id)


async def _other_owner(meter: Any, user_id: str, customer: str, subscription: dict[str, Any]) -> str | None:
    """Why a subscription listed under the account's Paddle customer is not this account's; None when it is.

    A Paddle customer is one payer email, and one payer can pay for several
    Remembra accounts. Evidence, strongest first: the account row that records
    the subscription id, then the ``custom_data.remembra_user_id`` the checkout
    wrote. With neither, the subscription is this account's only when no other
    account shares the customer.
    """
    holders = await meter.tenants_for_billing(subscription_id=str(subscription["id"]))
    if holders:
        return None if user_id in holders else "recorded on another account"
    named = (subscription.get("custom_data") or {}).get("remembra_user_id")
    if named:
        return None if str(named) == user_id else "its checkout names another account"
    sharing = [u for u in await meter.tenants_for_billing(customer_id=customer) if u != user_id]
    return "its Paddle customer also pays for another account" if sharing else None


async def cancel_billing(meter: Any | None, user_id: str, *, app_state: Any = None) -> BillingCancelOutcome:
    """Cancel every subscription of ``user_id`` that can still bill.

    Covers the subscription the account holds and any other billable
    subscription of its Paddle customer that is this account's (a duplicate
    purchase); one that belongs to another account sharing the customer is left
    alone and the owner is alerted. Cancels take effect immediately and are
    idempotent: an already-cancelled subscription counts as done, and so does
    one Paddle does not know (404, owner alerted). Raises
    :class:`BillingCancelError` (owner alerted) when Paddle cannot be asked or
    refuses, so the caller keeps the account.
    """
    outcome = BillingCancelOutcome()
    if meter is None:
        return outcome
    tenant = await meter.get_tenant(user_id) or {}
    held = meter.active_subscription_id(tenant)
    customer = tenant.get("stripe_customer_id")
    if not held and not customer:
        return outcome

    from remembra.api.v1.billing import get_billing_provider
    from remembra.config import get_settings

    if get_billing_provider(get_settings()) != "paddle":
        if held:
            await notify_owner(
                app_state,
                f"account_deletion_billing_failed:{user_id}",
                "An account deletion was refused: the server has no billing provider configured to cancel its subscription.",
                {"user_id": user_id, "subscription_id": held},
            )
            raise BillingCancelError(
                "This server has no billing provider configured, so the subscription cannot be cancelled here.",
                transient=False,
                subscription_id=held,
            )
        return outcome

    billing = _paddle_manager()
    subscriptions: list[str] = [held] if held else []
    current: str | None = None
    try:
        if customer:
            for sub in await billing.list_billable_subscriptions(str(customer)):
                sub_id = str(sub["id"])
                if sub_id in subscriptions:
                    continue
                reason = await _other_owner(meter, user_id, str(customer), sub)
                if reason is None:
                    subscriptions.append(sub_id)
                else:
                    outcome.other_accounts.append(sub_id)
                    log.warning("account_deletion_subscription_left", user_id=user_id, reason=reason)
        for sub_id in subscriptions:
            current = sub_id
            result = await billing.cancel_subscription_now(sub_id)
            if result == "not_found":
                outcome.not_found.append(sub_id)
            else:
                outcome.cancelled.append(sub_id)
            log.info("account_deletion_subscription_cancelled", user_id=user_id, outcome=result)
    except httpx.HTTPError as e:
        status_code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
        log.error("account_deletion_billing_cancel_failed", user_id=user_id, error_type=type(e).__name__, status=status_code)
        error = _cancel_error(e, current)
        await notify_owner(
            app_state,
            f"account_deletion_billing_failed:{user_id}",
            "An account deletion was refused because Paddle did not confirm the subscription cancel. "
            + ("It may clear on a retry." if error.transient else "Paddle refused it: check the subscription in Paddle."),
            {"user_id": user_id, "subscription_id": current, "paddle_status": status_code, "transient": error.transient},
        )
        raise error from e
    if outcome.not_found:
        await notify_owner(
            app_state,
            f"account_deletion_subscription_unknown:{user_id}",
            "A deleted account recorded a subscription id Paddle does not know. Nothing bills through Paddle under "
            "it; if it is a Stripe-era or hand-entered id, make sure nothing still charges this customer elsewhere.",
            {"user_id": user_id, "subscription_ids": outcome.not_found},
        )
    if outcome.other_accounts:
        await notify_owner(
            app_state,
            f"account_deletion_shared_customer:{user_id}",
            "A deleted account shares its Paddle customer with other accounts; their subscriptions were left "
            "running. Check in Paddle that each belongs to a live account.",
            {"user_id": user_id, "customer_id": customer, "subscription_ids": outcome.other_accounts},
        )
    return outcome


async def skip_billing(meter: Any | None, user_id: str, *, app_state: Any = None) -> None:
    """Superadmin escape hatch: delete without asking Paddle; the owner is told what to cancel by hand."""
    tenant = (await meter.get_tenant(user_id) or {}) if meter is not None else {}
    held = tenant.get("stripe_subscription_id")
    customer = tenant.get("stripe_customer_id")
    log.warning("account_deletion_billing_skipped", user_id=user_id)
    if held or customer:
        await notify_owner(
            app_state,
            f"account_deletion_billing_skipped:{user_id}",
            "An account was deleted with force_billing=skip, so Paddle was not asked to cancel anything. "
            "Cancel its subscriptions in Paddle by hand if they still bill.",
            {"user_id": user_id, "subscription_id": held, "customer_id": customer},
        )


async def mark_deleted(db: Any, meter: Any | None, user_id: str) -> datetime:
    """Deactivate the account, cut every session and key, end its paid plan; returns the deletion time."""
    from remembra.auth.users import revoke_user_access

    now = datetime.now(UTC)
    await db.conn.execute(
        "UPDATE users SET deleted_at = COALESCE(deleted_at, ?), is_active = FALSE, updated_at = ? WHERE id = ?",
        (now.isoformat(), now.isoformat(), user_id),
    )
    await db.conn.commit()
    await revoke_user_access(db, user_id)
    if meter is not None:
        tenant = await meter.get_tenant(user_id)
        if tenant is not None and str(tenant.get("plan") or PlanTier.FREE.value) != PlanTier.FREE.value:
            # Billing was cancelled above; the account no longer holds a paid plan.
            await meter.apply_subscription(user_id, PlanTier.FREE)
    cursor = await db.conn.execute("SELECT deleted_at FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    stamp = datetime.fromisoformat(str(row[0])) if row and row[0] else now
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


async def cancel_pending_deletion(db: Any, user_id: str) -> bool:
    """Undo a deletion inside its grace period (owner/support action). False when nothing is pending."""
    cursor = await db.conn.execute(
        "UPDATE users SET deleted_at = NULL, is_active = TRUE, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
        (datetime.now(UTC).isoformat(), user_id),
    )
    await db.conn.commit()
    return bool(cursor.rowcount)
