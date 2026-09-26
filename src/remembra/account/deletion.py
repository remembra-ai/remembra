"""Self-serve account deletion (R-11): confirm, stop billing, then schedule erasure.

Order matters. Billing is cancelled in Paddle FIRST: if Paddle cannot confirm
the cancel, nothing is deleted and the user is told to retry, so an account can
never be gone while its subscription keeps charging. Then the account is
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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from remembra.cloud.plans import PlanTier

log = structlog.get_logger(__name__)

DELETION_CODE_TTL = timedelta(minutes=15)
DELETION_CODE_MAX_ATTEMPTS = 5


class BillingCancelError(RuntimeError):
    """The subscription could not be confirmed cancelled; the account must not be deleted."""


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


async def cancel_billing(meter: Any | None, user_id: str) -> list[str]:
    """Cancel every subscription of ``user_id`` that can still bill; returns the ids now cancelled.

    Covers the subscription the account holds and any other billable
    subscription of its Paddle customer (a duplicate purchase). Cancels take
    effect immediately and are idempotent: an already-cancelled subscription
    counts as done. Raises :class:`BillingCancelError` when Paddle cannot be
    asked or refuses, so the caller keeps the account.
    """
    if meter is None:
        return []
    tenant = await meter.get_tenant(user_id) or {}
    held = meter.active_subscription_id(tenant)
    customer = tenant.get("stripe_customer_id")
    if not held and not customer:
        return []

    from remembra.api.v1.billing import get_billing_provider
    from remembra.config import get_settings

    if get_billing_provider(get_settings()) != "paddle":
        if held:
            raise BillingCancelError(
                "This server has no billing provider configured, so the subscription cannot be cancelled here."
            )
        return []

    billing = _paddle_manager()
    subscriptions: list[str] = [held] if held else []
    try:
        if customer:
            for sub in await billing.list_billable_subscriptions(str(customer)):
                if sub not in subscriptions:
                    subscriptions.append(sub)
        cancelled: list[str] = []
        for sub in subscriptions:
            outcome = await billing.cancel_subscription_now(sub)
            cancelled.append(sub)
            log.info("account_deletion_subscription_cancelled", user_id=user_id, outcome=outcome)
    except httpx.HTTPError as e:
        log.error("account_deletion_billing_cancel_failed", user_id=user_id, error_type=type(e).__name__)
        raise BillingCancelError(
            "We could not confirm with our billing provider that your subscription is cancelled, so your account "
            "was not deleted. Nothing changed; please try again in a few minutes."
        ) from e
    return cancelled


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
